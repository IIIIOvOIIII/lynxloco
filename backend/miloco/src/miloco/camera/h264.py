"""Small, bounded H.264 packet normalizer for browser live viewers."""

from __future__ import annotations

from dataclasses import dataclass

from miloco.camera.stream import EncodedVideoPacket

_START_CODE = b"\x00\x00\x00\x01"
_MAX_PACKET_BYTES = 8 * 1024 * 1024
_MAX_EXTRADATA_BYTES = 64 * 1024
_MAX_NAL_BYTES = 4 * 1024 * 1024
_MAX_NALS = 256
_BROWSER_SAFE_PROFILES = frozenset({66, 77, 100})
_MAX_BROWSER_SAFE_LEVEL = 40


@dataclass(frozen=True)
class H264Compatibility:
    passthrough: bool
    profile: int | None
    level: int | None
    reason: str


@dataclass(frozen=True)
class _AvcConfiguration:
    length_size: int
    sps: tuple[bytes, ...]
    pps: tuple[bytes, ...]
    profile: int
    level: int


class _MalformedH264(ValueError):
    pass


def _validate_nal(nal: bytes) -> None:
    if not nal or len(nal) > _MAX_NAL_BYTES:
        raise _MalformedH264
    header = nal[0]
    nal_type = header & 0x1F
    if header & 0x80 or not 1 <= nal_type <= 23:
        raise _MalformedH264


def _split_annexb(data: bytes) -> list[bytes]:
    if not data or len(data) > _MAX_PACKET_BYTES:
        raise _MalformedH264
    if data.startswith(_START_CODE):
        payload_start = 4
    elif data.startswith(b"\x00\x00\x01"):
        payload_start = 3
    else:
        raise _MalformedH264

    nals: list[bytes] = []
    while True:
        marker = data.find(b"\x00\x00\x01", payload_start)
        if marker < 0:
            nal = data[payload_start:]
            _validate_nal(nal)
            nals.append(nal)
            break

        marker_start = marker - 1 if marker > 0 and data[marker - 1] == 0 else marker
        nal = data[payload_start:marker_start]
        _validate_nal(nal)
        nals.append(nal)
        if len(nals) >= _MAX_NALS:
            raise _MalformedH264
        payload_start = marker + 3

    if len(nals) > _MAX_NALS:
        raise _MalformedH264
    return nals


def _split_length_prefixed(data: bytes, length_size: int) -> list[bytes]:
    if not data or len(data) > _MAX_PACKET_BYTES or length_size not in {1, 2, 4}:
        raise _MalformedH264
    offset = 0
    nals: list[bytes] = []
    while offset < len(data):
        if len(nals) >= _MAX_NALS or offset + length_size > len(data):
            raise _MalformedH264
        nal_size = int.from_bytes(data[offset : offset + length_size], "big")
        offset += length_size
        if nal_size <= 0 or nal_size > _MAX_NAL_BYTES or offset + nal_size > len(data):
            raise _MalformedH264
        nal = data[offset : offset + nal_size]
        _validate_nal(nal)
        nals.append(nal)
        offset += nal_size
    return nals


def _parse_avcc(extradata: bytes) -> _AvcConfiguration:
    if not 7 <= len(extradata) <= _MAX_EXTRADATA_BYTES or extradata[0] != 1:
        raise _MalformedH264
    if extradata[4] & 0xFC != 0xFC or extradata[5] & 0xE0 != 0xE0:
        raise _MalformedH264
    length_size = (extradata[4] & 0x03) + 1
    if length_size not in {1, 2, 4}:
        raise _MalformedH264

    offset = 6
    sps_count = extradata[5] & 0x1F
    if not 1 <= sps_count <= 31:
        raise _MalformedH264

    def read_nals(count: int, expected_type: int) -> tuple[bytes, ...]:
        nonlocal offset
        nals: list[bytes] = []
        for _ in range(count):
            if offset + 2 > len(extradata):
                raise _MalformedH264
            nal_size = int.from_bytes(extradata[offset : offset + 2], "big")
            offset += 2
            if (
                nal_size <= 0
                or nal_size > _MAX_NAL_BYTES
                or offset + nal_size > len(extradata)
            ):
                raise _MalformedH264
            nal = extradata[offset : offset + nal_size]
            offset += nal_size
            _validate_nal(nal)
            if nal[0] & 0x1F != expected_type:
                raise _MalformedH264
            nals.append(nal)
        return tuple(nals)

    sps = read_nals(sps_count, 7)
    if offset >= len(extradata):
        raise _MalformedH264
    pps_count = extradata[offset]
    offset += 1
    if not 1 <= pps_count <= 31:
        raise _MalformedH264
    pps = read_nals(pps_count, 8)
    if offset != len(extradata):
        raise _MalformedH264

    first_sps = sps[0]
    if len(first_sps) < 4:
        raise _MalformedH264
    profile = first_sps[1]
    level = first_sps[3]
    if profile != extradata[1] or level != extradata[3]:
        raise _MalformedH264
    return _AvcConfiguration(length_size, sps, pps, profile, level)


def _configuration_from_nals(
    nals: list[bytes], length_size: int
) -> _AvcConfiguration | None:
    sps = tuple(nal for nal in nals if nal[0] & 0x1F == 7)
    pps = tuple(nal for nal in nals if nal[0] & 0x1F == 8)
    if not sps and not pps:
        return None
    if (
        not sps
        or not pps
        or len(sps) > 31
        or len(pps) > 31
        or len(sps[0]) < 4
        or sum(map(len, sps + pps)) > _MAX_EXTRADATA_BYTES
    ):
        raise _MalformedH264
    return _AvcConfiguration(length_size, sps, pps, sps[0][1], sps[0][3])


def _to_annexb(nals: list[bytes] | tuple[bytes, ...]) -> bytes:
    return b"".join(_START_CODE + nal for nal in nals)


class H264AnnexBNormalizer:
    """Normalize one viewer's AVC packets and enforce an IDR startup boundary.

    Instances are intentionally viewer-scoped: each new viewer begins in the
    waiting state and receives cached decoder configuration immediately before
    its first IDR access unit.
    """

    def __init__(self) -> None:
        self._configuration: _AvcConfiguration | None = None
        self._viewer_started = False

    def _evaluate(
        self, packet: EncodedVideoPacket
    ) -> tuple[H264Compatibility, list[bytes] | None]:
        if packet.codec != "h264":
            return H264Compatibility(False, None, None, "codec_not_h264"), None
        try:
            extradata_configuration: _AvcConfiguration | None = None
            if packet.extradata:
                if len(packet.extradata) > _MAX_EXTRADATA_BYTES:
                    raise _MalformedH264
                if packet.extradata[0] == 1:
                    extradata_configuration = _parse_avcc(packet.extradata)
                else:
                    extra_nals = _split_annexb(packet.extradata)
                    extradata_configuration = _configuration_from_nals(extra_nals, 4)
                    if extradata_configuration is None:
                        raise _MalformedH264

            if packet.data.startswith((b"\x00\x00\x01", _START_CODE)):
                nals = _split_annexb(packet.data)
                packet_configuration = _configuration_from_nals(nals, 4)
            else:
                configuration = extradata_configuration or self._configuration
                if configuration is None:
                    return (
                        H264Compatibility(False, None, None, "configuration_missing"),
                        None,
                    )
                nals = _split_length_prefixed(packet.data, configuration.length_size)
                packet_configuration = _configuration_from_nals(
                    nals, configuration.length_size
                )

            configuration = packet_configuration or extradata_configuration
            if configuration is not None:
                self._configuration = configuration
            else:
                configuration = self._configuration
            if configuration is None:
                return (
                    H264Compatibility(False, None, None, "configuration_missing"),
                    nals,
                )
        except _MalformedH264:
            return H264Compatibility(False, None, None, "malformed_h264"), None

        profile = configuration.profile
        level = configuration.level
        if profile not in _BROWSER_SAFE_PROFILES:
            return H264Compatibility(False, profile, level, "profile_unsupported"), nals
        if level > _MAX_BROWSER_SAFE_LEVEL:
            return H264Compatibility(False, profile, level, "level_unsupported"), nals
        return H264Compatibility(True, profile, level, "compatible"), nals

    def inspect(self, packet: EncodedVideoPacket) -> H264Compatibility:
        compatibility, _ = self._evaluate(packet)
        return compatibility

    def push(self, packet: EncodedVideoPacket) -> list[bytes]:
        compatibility, nals = self._evaluate(packet)
        if not compatibility.passthrough or not nals:
            return []
        if not self._viewer_started:
            idr_index = next(
                (index for index, nal in enumerate(nals) if nal[0] & 0x1F == 5),
                None,
            )
            if idr_index is None:
                return []
            self._viewer_started = True
            return [self.decoder_config() + _to_annexb(nals[idr_index:])]
        return [_to_annexb(nals)]

    def decoder_config(self) -> bytes:
        if self._configuration is None:
            return b""
        return _to_annexb(self._configuration.sps + self._configuration.pps)
