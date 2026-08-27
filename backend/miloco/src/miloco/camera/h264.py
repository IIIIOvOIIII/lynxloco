"""Small, bounded H.264 packet normalizer for browser live viewers."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, log2

from miloco.camera.stream import EncodedVideoPacket

_START_CODE = b"\x00\x00\x00\x01"
_MAX_PACKET_BYTES = 8 * 1024 * 1024
_MAX_EXTRADATA_BYTES = 64 * 1024
_MAX_NAL_BYTES = 4 * 1024 * 1024
_MAX_NALS = 256
_BROWSER_SAFE_PROFILES = frozenset({66, 77, 100})
_BROWSER_SAFE_LEVELS = frozenset({10, 11, 12, 13, 20, 21, 22, 30, 31, 32, 40})
_HIGH_PROFILE_SYNTAX = frozenset(
    {44, 83, 86, 100, 110, 118, 122, 128, 134, 135, 138, 139, 244}
)


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


class _UnsupportedH264Configuration(ValueError):
    pass


@dataclass(frozen=True)
class _SpsSyntax:
    sps_id: int
    profile: int
    profile_compatibility: int
    level: int
    chroma_format_idc: int


class _BitReader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._position = 0

    def read_bit(self) -> int:
        if self._position >= len(self._data) * 8:
            raise _MalformedH264
        byte = self._data[self._position // 8]
        bit = (byte >> (7 - self._position % 8)) & 1
        self._position += 1
        return bit

    def read_bits(self, count: int) -> int:
        if count < 0 or count > 32:
            raise _MalformedH264
        value = 0
        for _ in range(count):
            value = (value << 1) | self.read_bit()
        return value

    def read_ue(self, maximum: int = (1 << 31) - 1) -> int:
        leading_zeros = 0
        while self.read_bit() == 0:
            leading_zeros += 1
            if leading_zeros > 31:
                raise _MalformedH264
        suffix = 0
        for _ in range(leading_zeros):
            suffix = (suffix << 1) | self.read_bit()
        value = (1 << leading_zeros) - 1 + suffix
        if value > maximum:
            raise _MalformedH264
        return value

    def read_se(self, maximum_absolute: int = (1 << 30) - 1) -> int:
        code_num = self.read_ue(maximum_absolute * 2)
        value = (code_num + 1) // 2 if code_num & 1 else -(code_num // 2)
        if abs(value) > maximum_absolute:
            raise _MalformedH264
        return value

    def more_rbsp_data(self) -> bool:
        remaining = len(self._data) * 8 - self._position
        if remaining <= 0:
            return False
        bits = [
            (self._data[position // 8] >> (7 - position % 8)) & 1
            for position in range(self._position, len(self._data) * 8)
        ]
        return not (bits[0] == 1 and all(bit == 0 for bit in bits[1:]))

    def consume_rbsp_trailing_bits(self) -> None:
        if self.read_bit() != 1:
            raise _MalformedH264
        while self._position % 8:
            if self.read_bit() != 0:
                raise _MalformedH264
        if self._position != len(self._data) * 8:
            raise _MalformedH264


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


def _parse_scaling_list(reader: _BitReader, size: int) -> None:
    last_scale = 8
    next_scale = 8
    for _ in range(size):
        if next_scale != 0:
            next_scale = (last_scale + reader.read_se(255) + 256) % 256
        last_scale = last_scale if next_scale == 0 else next_scale


def _parse_hrd(reader: _BitReader) -> None:
    cpb_count = reader.read_ue(31) + 1
    reader.read_bits(4)
    reader.read_bits(4)
    for _ in range(cpb_count):
        reader.read_ue()
        reader.read_ue()
        reader.read_bit()
    reader.read_bits(5)
    reader.read_bits(5)
    reader.read_bits(5)
    reader.read_bits(5)


def _parse_vui(reader: _BitReader) -> None:
    if reader.read_bit():
        aspect_ratio_idc = reader.read_bits(8)
        if aspect_ratio_idc == 255:
            reader.read_bits(16)
            reader.read_bits(16)
    if reader.read_bit():
        reader.read_bit()
    if reader.read_bit():
        reader.read_bits(3)
        reader.read_bit()
        if reader.read_bit():
            reader.read_bits(8)
            reader.read_bits(8)
            reader.read_bits(8)
    if reader.read_bit():
        reader.read_ue()
        reader.read_ue()
    if reader.read_bit():
        reader.read_bits(32)
        reader.read_bits(32)
        reader.read_bit()
    nal_hrd = bool(reader.read_bit())
    if nal_hrd:
        _parse_hrd(reader)
    vcl_hrd = bool(reader.read_bit())
    if vcl_hrd:
        _parse_hrd(reader)
    if nal_hrd or vcl_hrd:
        reader.read_bit()
    reader.read_bit()
    if reader.read_bit():
        reader.read_bit()
        reader.read_ue()
        reader.read_ue()
        reader.read_ue()
        reader.read_ue()
        reader.read_ue()
        reader.read_ue()


def _parse_sps(nal: bytes) -> _SpsSyntax:
    if len(nal) < 5 or nal[0] & 0x1F != 7:
        raise _MalformedH264
    rbsp = _rbsp(nal[1:])
    if len(rbsp) < 4:
        raise _MalformedH264
    profile, profile_compatibility, level = rbsp[:3]
    if profile_compatibility & 0x03:
        raise _MalformedH264
    reader = _BitReader(rbsp[3:])
    sps_id = reader.read_ue(31)
    chroma_format_idc = 1
    if profile in _HIGH_PROFILE_SYNTAX:
        chroma_format_idc = reader.read_ue(3)
        if chroma_format_idc == 3:
            reader.read_bit()
        reader.read_ue(6)
        reader.read_ue(6)
        reader.read_bit()
        if reader.read_bit():
            scaling_list_count = 8 if chroma_format_idc != 3 else 12
            for index in range(scaling_list_count):
                if reader.read_bit():
                    _parse_scaling_list(reader, 16 if index < 6 else 64)

    reader.read_ue(12)
    pic_order_count_type = reader.read_ue(2)
    if pic_order_count_type == 0:
        reader.read_ue(12)
    elif pic_order_count_type == 1:
        reader.read_bit()
        reader.read_se()
        reader.read_se()
        cycle_count = reader.read_ue(255)
        for _ in range(cycle_count):
            reader.read_se()
    reader.read_ue(65535)
    reader.read_bit()
    reader.read_ue(65535)
    reader.read_ue(65535)
    frame_mbs_only = bool(reader.read_bit())
    if not frame_mbs_only:
        reader.read_bit()
    reader.read_bit()
    if reader.read_bit():
        reader.read_ue(65535)
        reader.read_ue(65535)
        reader.read_ue(65535)
        reader.read_ue(65535)
    if reader.read_bit():
        _parse_vui(reader)
    reader.consume_rbsp_trailing_bits()
    return _SpsSyntax(
        sps_id,
        profile,
        profile_compatibility,
        level,
        chroma_format_idc,
    )


def _parse_pps(nal: bytes, chroma_format_idc: int) -> tuple[int, int]:
    if len(nal) < 2 or nal[0] & 0x1F != 8:
        raise _MalformedH264
    reader = _BitReader(_rbsp(nal[1:]))
    pps_id = reader.read_ue(255)
    sps_id = reader.read_ue(31)
    reader.read_bit()
    reader.read_bit()
    slice_group_count = reader.read_ue(7) + 1
    if slice_group_count > 1:
        map_type = reader.read_ue(6)
        if map_type == 0:
            for _ in range(slice_group_count):
                reader.read_ue(65535)
        elif map_type == 2:
            for _ in range(slice_group_count - 1):
                reader.read_ue(65535)
                reader.read_ue(65535)
        elif map_type in {3, 4, 5}:
            reader.read_bit()
            reader.read_ue(65535)
        elif map_type == 6:
            map_units = reader.read_ue(65535) + 1
            bits_per_id = ceil(log2(slice_group_count))
            for _ in range(map_units):
                if reader.read_bits(bits_per_id) >= slice_group_count:
                    raise _MalformedH264
        else:
            raise _MalformedH264
    reader.read_ue(31)
    reader.read_ue(31)
    reader.read_bit()
    reader.read_bits(2)
    reader.read_se(51)
    reader.read_se(51)
    reader.read_se(51)
    reader.read_bit()
    reader.read_bit()
    reader.read_bit()
    if reader.more_rbsp_data():
        transform_8x8_mode = bool(reader.read_bit())
        if reader.read_bit():
            extra_lists = (2 if chroma_format_idc != 3 else 6) * transform_8x8_mode
            for index in range(6 + extra_lists):
                if reader.read_bit():
                    _parse_scaling_list(reader, 16 if index < 6 else 64)
        reader.read_se(51)
    reader.consume_rbsp_trailing_bits()
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
        len(sps) != 1
        or len(pps) != 1
        or sum(map(len, sps + pps)) > _MAX_EXTRADATA_BYTES
    ):
        raise _UnsupportedH264Configuration

    syntax = _parse_sps(sps[0])
    _, referenced_sps_id = _parse_pps(pps[0], syntax.chroma_format_idc)
    if referenced_sps_id != syntax.sps_id:
        raise _MalformedH264
    return _AvcConfiguration(
        length_size,
        sps,
        pps,
        syntax.profile,
        syntax.profile_compatibility,
        syntax.level,
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
        except _UnsupportedH264Configuration:
            return (
                H264Compatibility(False, None, None, "configuration_unsupported"),
                None,
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
