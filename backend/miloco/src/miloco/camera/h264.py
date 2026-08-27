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
_BROWSER_SAFE_LEVELS = frozenset({10, 11, 12, 13, 20, 21, 22, 30, 31, 32, 40})


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
    profile_compatibility: int
    level: int


class _MalformedH264(ValueError):
    pass


class _BitReader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._position = 0

    def _read_bit(self) -> int:
        if self._position >= len(self._data) * 8:
            raise _MalformedH264
        byte = self._data[self._position // 8]
        bit = (byte >> (7 - self._position % 8)) & 1
        self._position += 1
        return bit

    def read_ue(self) -> int:
        leading_zeros = 0
        while self._read_bit() == 0:
            leading_zeros += 1
            if leading_zeros > 31:
                raise _MalformedH264
        suffix = 0
        for _ in range(leading_zeros):
            suffix = (suffix << 1) | self._read_bit()
        return (1 << leading_zeros) - 1 + suffix


def _rbsp(ebsp: bytes) -> bytes:
    if not ebsp:
        raise _MalformedH264
    result = bytearray()
    index = 0
    while index < len(ebsp):
        if index + 2 < len(ebsp) and ebsp[index : index + 2] == b"\x00\x00":
            following = ebsp[index + 2]
            if following == 3:
                if index + 3 >= len(ebsp) or ebsp[index + 3] > 3:
                    raise _MalformedH264
                result.extend(b"\x00\x00")
                index += 3
                continue
            if following <= 2:
                raise _MalformedH264
        result.append(ebsp[index])
        index += 1
    return bytes(result)


def _parse_sps(nal: bytes) -> tuple[int, int, int, int]:
    if len(nal) < 5 or nal[0] & 0x1F != 7:
        raise _MalformedH264
    rbsp = _rbsp(nal[1:])
    if len(rbsp) < 4:
        raise _MalformedH264
    profile, profile_compatibility, level = rbsp[:3]
    if profile_compatibility & 0x03:
        raise _MalformedH264
    sps_id = _BitReader(rbsp[3:]).read_ue()
    if sps_id > 31:
        raise _MalformedH264
    return sps_id, profile, profile_compatibility, level


def _parse_pps(nal: bytes) -> tuple[int, int]:
    if len(nal) < 2 or nal[0] & 0x1F != 8:
        raise _MalformedH264
    reader = _BitReader(_rbsp(nal[1:]))
    pps_id = reader.read_ue()
    sps_id = reader.read_ue()
    if pps_id > 255 or sps_id > 31:
        raise _MalformedH264
    return pps_id, sps_id


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

    configuration = _build_configuration(length_size, sps, pps)
    if (
        configuration.profile != extradata[1]
        or configuration.profile_compatibility != extradata[2]
        or configuration.level != extradata[3]
    ):
        raise _MalformedH264
    return configuration


def _build_configuration(
    length_size: int, sps: tuple[bytes, ...], pps: tuple[bytes, ...]
) -> _AvcConfiguration:
    if (
        not sps
        or not pps
        or len(sps) > 31
        or len(pps) > 31
        or sum(map(len, sps + pps)) > _MAX_EXTRADATA_BYTES
    ):
        raise _MalformedH264

    sps_by_id: dict[int, bytes] = {}
    common_format: tuple[int, int, int] | None = None
    for nal in sps:
        sps_id, profile, profile_compatibility, level = _parse_sps(nal)
        stream_format = (profile, profile_compatibility, level)
        if common_format is None:
            common_format = stream_format
        elif stream_format != common_format:
            raise _MalformedH264
        prior = sps_by_id.get(sps_id)
        if prior is not None and prior != nal:
            raise _MalformedH264
        sps_by_id[sps_id] = nal

    pps_by_id: dict[int, tuple[int, bytes]] = {}
    for nal in pps:
        pps_id, referenced_sps_id = _parse_pps(nal)
        if referenced_sps_id not in sps_by_id:
            raise _MalformedH264
        prior = pps_by_id.get(pps_id)
        current = (referenced_sps_id, nal)
        if prior is not None and prior != current:
            raise _MalformedH264
        pps_by_id[pps_id] = current

    if common_format is None:
        raise _MalformedH264
    profile, profile_compatibility, level = common_format
    return _AvcConfiguration(
        length_size,
        sps,
        pps,
        profile,
        profile_compatibility,
        level,
    )


def _configuration_from_nals(
    nals: list[bytes], length_size: int
) -> _AvcConfiguration | None:
    sps = tuple(nal for nal in nals if nal[0] & 0x1F == 7)
    pps = tuple(nal for nal in nals if nal[0] & 0x1F == 8)
    if not sps and not pps:
        return None
    return _build_configuration(length_size, sps, pps)


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
    ) -> tuple[H264Compatibility, list[bytes] | None, _AvcConfiguration | None]:
        if packet.codec != "h264":
            return (
                H264Compatibility(False, None, None, "codec_not_h264"),
                None,
                None,
            )
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
                        None,
                    )
                nals = _split_length_prefixed(packet.data, configuration.length_size)
                packet_configuration = _configuration_from_nals(
                    nals, configuration.length_size
                )

            if packet_configuration is not None and extradata_configuration is not None:
                if (
                    packet_configuration.sps != extradata_configuration.sps
                    or packet_configuration.pps != extradata_configuration.pps
                    or packet_configuration.profile != extradata_configuration.profile
                    or packet_configuration.profile_compatibility
                    != extradata_configuration.profile_compatibility
                    or packet_configuration.level != extradata_configuration.level
                ):
                    raise _MalformedH264
                configuration = extradata_configuration
            else:
                configuration = (
                    packet_configuration
                    or extradata_configuration
                    or self._configuration
                )
            if configuration is None:
                return (
                    H264Compatibility(False, None, None, "configuration_missing"),
                    nals,
                    None,
                )
        except _MalformedH264:
            return (
                H264Compatibility(False, None, None, "malformed_h264"),
                None,
                None,
            )

        profile = configuration.profile
        level = configuration.level
        if profile not in _BROWSER_SAFE_PROFILES:
            return (
                H264Compatibility(False, profile, level, "profile_unsupported"),
                nals,
                configuration,
            )
        if level not in _BROWSER_SAFE_LEVELS:
            return (
                H264Compatibility(False, profile, level, "level_unsupported"),
                nals,
                configuration,
            )
        return (
            H264Compatibility(True, profile, level, "compatible"),
            nals,
            configuration,
        )

    def inspect(self, packet: EncodedVideoPacket) -> H264Compatibility:
        compatibility, _, _ = self._evaluate(packet)
        return compatibility

    def push(self, packet: EncodedVideoPacket) -> list[bytes]:
        compatibility, nals, configuration = self._evaluate(packet)
        if not compatibility.passthrough or not nals or configuration is None:
            self._viewer_started = False
            return []
        if configuration != self._configuration:
            self._configuration = configuration
            self._viewer_started = False
        if not self._viewer_started:
            idr_index = next(
                (index for index, nal in enumerate(nals) if nal[0] & 0x1F == 5),
                None,
            )
            if idr_index is None:
                return []
            self._viewer_started = True
            pre_idr_non_vcl = [
                nal for nal in nals[:idr_index] if nal[0] & 0x1F not in {1, 5, 7, 8}
            ]
            return [
                self.decoder_config()
                + _to_annexb(pre_idr_non_vcl)
                + _to_annexb(nals[idr_index:])
            ]
        return [_to_annexb(nals)]

    def decoder_config(self) -> bytes:
        if self._configuration is None:
            return b""
        return _to_annexb(self._configuration.sps + self._configuration.pps)
