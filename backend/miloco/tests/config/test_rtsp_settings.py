# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""RTSP 摄像机来源配置契约测试。"""

from __future__ import annotations

import pytest
from miloco.config.settings import CameraSettings, RtspSourceSettings
from pydantic import ValidationError


def _source(**overrides: object) -> RtspSourceSettings:
    data: dict[str, object] = {
        "id": "rtsp:living-room",
        "name": "Living room",
        "uri": "rtsp://camera.local:554/stream1",
    }
    data.update(overrides)
    return RtspSourceSettings(**data)


@pytest.mark.parametrize(
    "uri", ["rtsp://camera.local:554/stream1", "rtsps://camera.local/stream1"]
)
def test_rtsp_source_accepts_rtsp_and_rtsps_uris(uri: str) -> None:
    assert _source(uri=uri).uri == uri


@pytest.mark.parametrize("uri", ["http://camera.local/stream1", "https://camera.local/stream1"])
def test_rtsp_source_rejects_unsupported_uri_schemes(uri: str) -> None:
    with pytest.raises(ValidationError):
        _source(uri=uri)


@pytest.mark.parametrize(
    "uri",
    [
        "rtsp://user:password@camera.local/stream1",
        "rtsp://camera.local/stream1#fragment",
        "rtsp:///stream1",
        "rtsp://camera.local/stream\n1",
    ],
)
def test_rtsp_source_rejects_unsafe_or_incomplete_uris(uri: str) -> None:
    with pytest.raises(ValidationError):
        _source(uri=uri)


def test_rtsp_source_requires_rtsp_prefixed_id() -> None:
    with pytest.raises(ValidationError):
        _source(id="living-room")


def test_camera_rejects_duplicate_rtsp_source_ids() -> None:
    with pytest.raises(ValidationError):
        CameraSettings(rtsp_sources=[_source(), _source(name="Kitchen")])


def test_rtsp_source_is_disabled_by_default() -> None:
    source = _source()

    assert source.enabled is False
    assert CameraSettings().rtsp_sources == []
