from __future__ import annotations

from pathlib import Path
from typing import Literal

import av
import pytest
from miloco.camera.h264 import H264AnnexBNormalizer, H264Compatibility
from miloco.camera.stream import EncodedVideoPacket

FIXTURES = Path(__file__).parents[1] / "fixtures" / "rtsp"
START_CODE = b"\x00\x00\x00\x01"
SPS = bytes.fromhex("6742c01eda11ec0440000003004000000523c58ba8")
PPS = bytes.fromhex("68ce0fc8")
HIGH_SPS = bytes.fromhex("6764000aacd9447a10000003001000000303c0f1225960")
HIGH_PPS = bytes.fromhex("68ebe3cb22c0")
P_SLICE = bytes.fromhex("419a22")
AVCC = bytes.fromhex("0142c01effe10015") + SPS + bytes.fromhex("010004") + PPS


def _length_prefixed_nals(data: bytes, length_size: int = 4) -> list[bytes]:
    nals: list[bytes] = []
    offset = 0
    while offset < len(data):
        size = int.from_bytes(data[offset : offset + length_size], "big")
        offset += length_size
        nals.append(data[offset : offset + size])
        offset += size
    return nals


REAL_AVCC_PACKET = (FIXTURES / "h264_avcc_packets.bin").read_bytes()
REAL_PACKET_NALS = _length_prefixed_nals(REAL_AVCC_PACKET)
IDR_SLICE = next(nal for nal in REAL_PACKET_NALS if nal[0] & 0x1F == 5)
REAL_NON_VCL = tuple(nal for nal in REAL_PACKET_NALS if nal[0] & 0x1F not in {1, 5})


def _avcc_extradata(
    *,
    sps: bytes = SPS,
    pps: bytes = PPS,
    profile: int = 0x42,
    profile_compatibility: int = 0xC0,
    level: int = 0x1E,
    length_size: int = 4,
) -> bytes:
    assert length_size in {1, 2, 4}
    configured_sps = bytearray(sps)
    configured_sps[1] = profile
    configured_sps[2] = profile_compatibility
    configured_sps[3] = level
    sps = bytes(configured_sps)
    return b"".join(
        (
            bytes(
                (
                    1,
                    profile,
                    profile_compatibility,
                    level,
                    0xFC | (length_size - 1),
                    0xE1,
                )
            ),
            len(sps).to_bytes(2, "big"),
            sps,
            b"\x01",
            len(pps).to_bytes(2, "big"),
            pps,
        )
    )


def _avcc_with_parameter_sets(
    sps: tuple[bytes, ...],
    pps: tuple[bytes, ...],
    *,
    profile: int,
    profile_compatibility: int,
    level: int,
) -> bytes:
    return b"".join(
        (
            bytes(
                (
                    1,
                    profile,
                    profile_compatibility,
                    level,
                    0xFF,
                    0xE0 | len(sps),
                )
            ),
            *(len(nal).to_bytes(2, "big") + nal for nal in sps),
            bytes((len(pps),)),
            *(len(nal).to_bytes(2, "big") + nal for nal in pps),
        )
    )


def _packet(
    data: bytes,
    *,
    codec: Literal["h264", "hevc"] = "h264",
    keyframe: bool = False,
    extradata: bytes = b"",
) -> EncodedVideoPacket:
    return EncodedVideoPacket(
        codec=codec,
        data=data,
        pts=1,
        dts=1,
        is_keyframe=keyframe,
        time_base_num=1,
        time_base_den=90000,
        extradata=extradata,
    )


def test_annexb_input_is_preserved_and_slice_bytes_are_unchanged() -> None:
    payload = (FIXTURES / "h264_annexb_packets.bin").read_bytes()
    normalizer = H264AnnexBNormalizer()

    compatibility = normalizer.inspect(_packet(payload, keyframe=True))
    output = normalizer.push(_packet(payload, keyframe=True))

    assert compatibility == H264Compatibility(True, 0x42, 0x1E, "compatible")
    assert output == [payload]
    assert IDR_SLICE in output[0]


def test_avcc_length_prefixes_are_converted_without_changing_nal_payloads() -> None:
    payload = (FIXTURES / "h264_avcc_packets.bin").read_bytes()
    normalizer = H264AnnexBNormalizer()

    output = normalizer.push(
        _packet(payload, keyframe=True, extradata=_avcc_extradata())
    )

    assert output == [
        START_CODE
        + SPS
        + START_CODE
        + PPS
        + b"".join(START_CODE + nal for nal in REAL_NON_VCL)
        + START_CODE
        + IDR_SLICE
    ]


def test_avcc_extradata_supplies_safe_annexb_decoder_configuration() -> None:
    normalizer = H264AnnexBNormalizer()
    packet = _packet(
        len(IDR_SLICE).to_bytes(2, "big") + IDR_SLICE,
        keyframe=True,
        extradata=_avcc_extradata(length_size=2),
    )

    assert normalizer.inspect(packet) == H264Compatibility(
        True, 0x42, 0x1E, "compatible"
    )
    assert normalizer.push(packet) == [
        START_CODE + SPS + START_CODE + PPS + START_CODE + IDR_SLICE
    ]
    assert normalizer.decoder_config() == START_CODE + SPS + START_CODE + PPS


def test_new_viewer_waits_for_idr_then_receives_decoder_config_first() -> None:
    normalizer = H264AnnexBNormalizer()
    extradata = _avcc_extradata()

    assert (
        normalizer.push(
            _packet(len(P_SLICE).to_bytes(4, "big") + P_SLICE, extradata=extradata)
        )
        == []
    )
    assert normalizer.push(
        _packet(
            len(IDR_SLICE).to_bytes(4, "big") + IDR_SLICE,
            keyframe=True,
            extradata=extradata,
        )
    ) == [START_CODE + SPS + START_CODE + PPS + START_CODE + IDR_SLICE]
    assert normalizer.push(
        _packet(len(P_SLICE).to_bytes(4, "big") + P_SLICE, extradata=extradata)
    ) == [START_CODE + P_SLICE]


@pytest.mark.parametrize(
    ("data", "extradata"),
    [
        (b"\x00\x00\x00\x05abc", _avcc_extradata()),
        (b"\x00\x00\x00\x00", _avcc_extradata()),
        (START_CODE + b"", b""),
        (START_CODE + b"\x00bad", b""),
        (START_CODE + b"\x80bad", b""),
        (START_CODE + b"\x67\x42\xc0\x1e" + START_CODE + PPS, b""),
        (START_CODE + SPS + START_CODE + b"\x68", b""),
        (START_CODE + SPS, _avcc_extradata()[:-1]),
        (b"\x00\x00\x10\x00x", _avcc_extradata()),
    ],
)
def test_malformed_lengths_nals_and_avcc_are_safely_rejected(
    data: bytes, extradata: bytes
) -> None:
    normalizer = H264AnnexBNormalizer()
    packet = _packet(data, keyframe=True, extradata=extradata)

    assert normalizer.inspect(packet).passthrough is False
    assert normalizer.inspect(packet).reason == "malformed_h264"
    assert normalizer.push(packet) == []


def test_declared_oversized_nal_and_excessive_nal_count_are_rejected() -> None:
    oversized = (9 * 1024 * 1024).to_bytes(4, "big") + b"x"
    too_many = b"".join(START_CODE + b"\x09" for _ in range(257))
    oversized_decoder_config = (
        START_CODE
        + b"\x67\x42\x00\x1f"
        + bytes(64 * 1024)
        + START_CODE
        + PPS
        + START_CODE
        + IDR_SLICE
    )

    for packet in (
        _packet(oversized, extradata=_avcc_extradata()),
        _packet(too_many, extradata=_avcc_extradata()),
        _packet(oversized_decoder_config, keyframe=True),
    ):
        normalizer = H264AnnexBNormalizer()
        assert normalizer.inspect(packet).passthrough is False
        assert normalizer.push(packet) == []


def test_non_h264_packet_is_rejected() -> None:
    normalizer = H264AnnexBNormalizer()

    compatibility = normalizer.inspect(
        _packet(START_CODE + IDR_SLICE, codec="hevc", keyframe=True)
    )

    assert compatibility == H264Compatibility(False, None, None, "codec_not_h264")


def test_inspect_is_pure_and_does_not_prime_decoder_configuration() -> None:
    normalizer = H264AnnexBNormalizer()
    packet = _packet(
        (FIXTURES / "h264_avcc_packets.bin").read_bytes(),
        keyframe=True,
        extradata=AVCC,
    )

    assert normalizer.inspect(packet).passthrough is True
    assert normalizer.decoder_config() == b""


@pytest.mark.parametrize(
    "extradata",
    [
        bytes((1, 0x42, 0xC0, 0x1E, 0xFB, 0xE1))
        + len(SPS).to_bytes(2, "big")
        + SPS
        + b"\x01"
        + len(PPS).to_bytes(2, "big")
        + PPS,
        bytes((1, 0x42, 0xC0, 0x1E, 0xFF, 0xC1))
        + len(SPS).to_bytes(2, "big")
        + SPS
        + b"\x01"
        + len(PPS).to_bytes(2, "big")
        + PPS,
        _avcc_extradata(profile_compatibility=0xC1),
        bytes((1, 0x42, 0x80, 0x1E)) + AVCC[4:],
    ],
)
def test_reserved_constraint_bits_and_avcc_header_mismatch_are_rejected(
    extradata: bytes,
) -> None:
    compatibility = H264AnnexBNormalizer().inspect(
        _packet(
            (FIXTURES / "h264_avcc_packets.bin").read_bytes(),
            keyframe=True,
            extradata=extradata,
        )
    )

    assert compatibility == H264Compatibility(False, None, None, "malformed_h264")


def test_pps_must_reference_a_present_sps() -> None:
    # ue(0) PPS id followed by ue(1) referenced SPS id, then rbsp stop bit.
    pps_referencing_sps_one = b"\x68\xa0"
    compatibility = H264AnnexBNormalizer().inspect(
        _packet(
            (FIXTURES / "h264_avcc_packets.bin").read_bytes(),
            keyframe=True,
            extradata=_avcc_extradata(pps=pps_referencing_sps_one),
        )
    )

    assert compatibility.reason == "malformed_h264"


def test_all_sps_must_have_one_supported_non_conflicting_format() -> None:
    unsupported_sps_zero = b"\x67\x6e\xc0\x1e\x80"
    unsupported_sps_one = b"\x67\x6e\xc0\x1e\x50"
    mixed_main_sps_one = b"\x67\x4d\xc0\x1e\x50"
    pps_zero_to_sps_zero = b"\x68\xc0"
    packet = (FIXTURES / "h264_avcc_packets.bin").read_bytes()

    unsupported = _avcc_with_parameter_sets(
        (unsupported_sps_zero, unsupported_sps_one),
        (pps_zero_to_sps_zero,),
        profile=0x6E,
        profile_compatibility=0xC0,
        level=0x1E,
    )
    conflicting = _avcc_with_parameter_sets(
        (SPS, mixed_main_sps_one),
        (pps_zero_to_sps_zero,),
        profile=0x42,
        profile_compatibility=0xC0,
        level=0x1E,
    )

    assert H264AnnexBNormalizer().inspect(
        _packet(packet, keyframe=True, extradata=unsupported)
    ) == H264Compatibility(False, None, None, "configuration_unsupported")
    assert (
        H264AnnexBNormalizer()
        .inspect(_packet(packet, keyframe=True, extradata=conflicting))
        .reason
        == "configuration_unsupported"
    )


def test_truncated_or_trailing_parameter_set_syntax_is_rejected() -> None:
    packet = (FIXTURES / "h264_avcc_packets.bin").read_bytes()
    malformed_records = [
        *(
            _avcc_with_parameter_sets(
                (SPS[:end],),
                (PPS,),
                profile=0x42,
                profile_compatibility=0xC0,
                level=0x1E,
            )
            for end in range(1, len(SPS))
        ),
        *(
            _avcc_with_parameter_sets(
                (SPS,),
                (PPS[:end],),
                profile=0x42,
                profile_compatibility=0xC0,
                level=0x1E,
            )
            for end in range(1, len(PPS))
        ),
        _avcc_extradata(sps=SPS + b"\x00"),
        _avcc_extradata(pps=PPS + b"\x00"),
    ]

    for extradata in malformed_records:
        assert (
            H264AnnexBNormalizer()
            .inspect(_packet(packet, keyframe=True, extradata=extradata))
            .passthrough
            is False
        )


def test_high_profile_required_syntax_is_consumed_to_rbsp_trailing_bits() -> None:
    extradata = _avcc_extradata(
        sps=HIGH_SPS,
        pps=HIGH_PPS,
        profile=100,
        profile_compatibility=0,
        level=10,
    )

    assert H264AnnexBNormalizer().inspect(
        _packet(START_CODE + P_SLICE, extradata=extradata)
    ) == H264Compatibility(True, 100, 10, "compatible")


def test_multiple_real_parameter_sets_are_ambiguous_for_jmuxer_v1() -> None:
    ambiguous = _avcc_with_parameter_sets(
        (SPS, HIGH_SPS),
        (PPS, HIGH_PPS),
        profile=0x42,
        profile_compatibility=0xC0,
        level=0x1E,
    )

    assert H264AnnexBNormalizer().inspect(
        _packet(REAL_AVCC_PACKET, keyframe=True, extradata=ambiguous)
    ) == H264Compatibility(False, None, None, "configuration_unsupported")


def test_bad_packet_resets_viewer_until_a_new_idr() -> None:
    normalizer = H264AnnexBNormalizer()
    idr = (FIXTURES / "h264_avcc_packets.bin").read_bytes()

    assert normalizer.push(_packet(idr, keyframe=True, extradata=AVCC))
    assert normalizer.push(_packet(b"\x00\x00\x00\xffbad")) == []
    assert normalizer.push(_packet(len(P_SLICE).to_bytes(4, "big") + P_SLICE)) == []
    recovered = normalizer.push(_packet(idr, keyframe=True))
    assert recovered and recovered[0].startswith(normalizer.decoder_config())


def test_unsupported_configuration_resets_viewer_until_a_new_idr() -> None:
    normalizer = H264AnnexBNormalizer()
    idr = (FIXTURES / "h264_avcc_packets.bin").read_bytes()

    assert normalizer.push(_packet(idr, keyframe=True, extradata=AVCC))
    assert (
        normalizer.push(
            _packet(
                len(P_SLICE).to_bytes(4, "big") + P_SLICE,
                extradata=_avcc_extradata(profile=0x6E),
            )
        )
        == []
    )
    assert normalizer.push(_packet(len(P_SLICE).to_bytes(4, "big") + P_SLICE)) == []
    assert normalizer.push(_packet(idr, keyframe=True))


def test_decoder_configuration_change_waits_for_idr_and_injects_new_config() -> None:
    normalizer = H264AnnexBNormalizer()
    idr = (FIXTURES / "h264_avcc_packets.bin").read_bytes()
    new_extradata = _avcc_extradata(
        sps=HIGH_SPS,
        pps=HIGH_PPS,
        profile=100,
        profile_compatibility=0,
        level=10,
    )

    assert normalizer.push(_packet(idr, keyframe=True, extradata=AVCC))
    assert (
        normalizer.push(
            _packet(
                len(P_SLICE).to_bytes(4, "big") + P_SLICE,
                extradata=new_extradata,
            )
        )
        == []
    )
    recovered = normalizer.push(_packet(idr, keyframe=True))
    assert recovered and recovered[0].startswith(
        START_CODE + HIGH_SPS + START_CODE + HIGH_PPS
    )


def test_real_annexb_fixture_decodes_at_least_one_frame_with_public_pyav() -> None:
    decoder = av.CodecContext.create("h264", "r")
    frames: list[av.VideoFrame] = []

    for packet in decoder.parse((FIXTURES / "h264_annexb_packets.bin").read_bytes()):
        frames.extend(decoder.decode(packet))
    for packet in decoder.parse(b""):
        frames.extend(decoder.decode(packet))
    frames.extend(decoder.decode(None))

    assert frames
    assert frames[0].width == 64
    assert frames[0].height == 48


@pytest.mark.parametrize(
    ("extradata", "expected"),
    [
        (b"", H264Compatibility(False, None, None, "configuration_missing")),
        (
            _avcc_extradata(
                sps=bytes((0x67, 0x6E)) + HIGH_SPS[2:],
                pps=HIGH_PPS,
                profile=0x6E,
                profile_compatibility=0,
                level=10,
            ),
            H264Compatibility(False, 0x6E, 10, "profile_unsupported"),
        ),
        (
            _avcc_extradata(level=0x29),
            H264Compatibility(False, 0x42, 0x29, "level_unsupported"),
        ),
        (
            _avcc_extradata(level=0),
            H264Compatibility(False, 0x42, 0, "level_unsupported"),
        ),
    ],
)
def test_compatibility_reasons_are_deterministic(
    extradata: bytes, expected: H264Compatibility
) -> None:
    packet = _packet(START_CODE + P_SLICE, extradata=extradata)

    assert H264AnnexBNormalizer().inspect(packet) == expected
    assert H264AnnexBNormalizer().inspect(packet) == expected
