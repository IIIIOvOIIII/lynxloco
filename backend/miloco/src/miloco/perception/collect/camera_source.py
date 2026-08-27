"""Camera source boundary shared by MIoT and RTSP transports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Protocol

import numpy as np
from numpy.typing import NDArray

from miloco.perception.types import PerceptionDevice

VideoFrameCallback = Callable[
    [str, NDArray[np.uint8], int, int, int, int], Awaitable[None]
]
AudioFrameCallback = Callable[
    [str, NDArray[np.int16], int, int, int, int], Awaitable[None]
]


@dataclass(frozen=True)
class CameraSourceState:
    connected: bool
    video_codec: str | None = None
    audio_codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    last_frame_unix_ms: int | None = None
    reconnect_attempt: int = 0
    dropped_frames: int = 0
    error_code: str | None = None
    error_message: str | None = None


class CameraSourceDriver(Protocol):
    source_type: Literal["miot", "rtsp"]

    async def discover_devices(
        self, all_devices: dict | None = None, **filters: object
    ) -> dict[str, PerceptionDevice]: ...

    async def connect_device(
        self,
        did: str,
        video_cb: VideoFrameCallback,
        audio_cb: AudioFrameCallback,
    ) -> None: ...

    async def disconnect_device(self, did: str) -> None: ...

    def get_state(self, did: str) -> CameraSourceState: ...

    async def shutdown(self) -> None: ...
