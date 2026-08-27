from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
from miloco.camera.h264 import H264AnnexBNormalizer, H264Compatibility
from miloco.camera.stream import EncodedVideoPacket

FIXTURES = Path(__file__).parents[1] / "fixtures" / "rtsp"
START_CODE = b"\x00\x00\x00\x01"
SPS = bytes.fromhex("6742c01fda014016ec0440000003004000000c83c60c6580")
PPS = bytes.fromhex("68ce3c80")
P_SLICE = bytes.fromhex("419a22")
IDR_SLICE = bytes.fromhex("658884")


def _avcc_extradata(
    *,
    sps: bytes = SPS,
    pps: bytes = PPS,
    profile: int = 0x42,
    level: int = 0x1F,
    length_size: int = 4,
) -> bytes:
    assert length_size in {1, 2, 4}
    configured_sps = bytearray(sps)
    configured_sps[1] = profile
    configured_sps[3] = level
    sps = bytes(configured_sps)
    return b"".join(
        (
            bytes((1, profile, 0xC0, level, 0xFC | (length_size - 1), 0xE1)),
            len(sps).to_bytes(2, "big"),
            sps,
            b"\x01",
            len(pps).to_bytes(2, "big"),
            pps,
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

    assert compatibility == H264Compatibility(True, 0x42, 0x1F, "compatible")
    assert output == [payload]
    assert IDR_SLICE in output[0]


def test_avcc_length_prefixes_are_converted_without_changing_nal_payloads() -> None:
    payload = (FIXTURES / "h264_avcc_packets.bin").read_bytes()
    normalizer = H264AnnexBNormalizer()

    output = normalizer.push(
        _packet(payload, keyframe=True, extradata=_avcc_extradata())
    )

    assert output == [START_CODE + SPS + START_CODE + PPS + START_CODE + IDR_SLICE]


def test_avcc_extradata_supplies_safe_annexb_decoder_configuration() -> None:
    normalizer = H264AnnexBNormalizer()
    packet = _packet(
        len(IDR_SLICE).to_bytes(2, "big") + IDR_SLICE,
        keyframe=True,
        extradata=_avcc_extradata(length_size=2),
    )

    assert normalizer.inspect(packet) == H264Compatibility(
        True, 0x42, 0x1F, "compatible"
    )
    assert normalizer.decoder_config() == START_CODE + SPS + START_CODE + PPS
    assert normalizer.push(packet) == [
        START_CODE + SPS + START_CODE + PPS + START_CODE + IDR_SLICE
    ]


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
        assert normalizer.inspect(packet).reason == "malformed_h264"
        assert normalizer.push(packet) == []


def test_non_h264_packet_is_rejected() -> None:
    normalizer = H264AnnexBNormalizer()

    compatibility = normalizer.inspect(
        _packet(START_CODE + IDR_SLICE, codec="hevc", keyframe=True)
    )

    assert compatibility == H264Compatibility(False, None, None, "codec_not_h264")


@pytest.mark.parametrize(
    ("extradata", "expected"),
    [
        (b"", H264Compatibility(False, None, None, "configuration_missing")),
        (
            _avcc_extradata(profile=0x6E),
            H264Compatibility(False, 0x6E, 0x1F, "profile_unsupported"),
        ),
        (
            _avcc_extradata(level=0x29),
            H264Compatibility(False, 0x42, 0x29, "level_unsupported"),
        ),
    ],
)
def test_compatibility_reasons_are_deterministic(
    extradata: bytes, expected: H264Compatibility
) -> None:
    packet = _packet(START_CODE + P_SLICE, extradata=extradata)

    assert H264AnnexBNormalizer().inspect(packet) == expected
    assert H264AnnexBNormalizer().inspect(packet) == expected
