"""
Camera device adapter — manages decoded video/audio frame streams from cameras.

Receives 2 decoded stream types per device from camera source drivers:
  1. decoded_video — decoded PyAV VideoFrame
  2. decoded_audio — decoded PyAV AudioFrame

Buffers fragments in a 2-track MultiTrackSyncBuffer per device. The sync
buffer handles time-windowed A/V alignment automatically.

Multi-channel cameras (dual-lens / NVR) expose each lens as a separate
perception unit. A single-lens camera keeps its bare did; each extra channel
gets a synthetic did ``{did}:ch{n}`` so downstream keying (device_results,
tracking, identity) never collides across lenses. The synthetic did is the key
that flows through discover / connect / disconnect / collect. Source-specific
physical identities remain inside their source driver.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from miloco.config import get_settings
from miloco.node_monitor import NodeName, get_monitor
from miloco.perception.collect.adapter_base import BaseDeviceAdapter
from miloco.perception.collect.camera_source import CameraSourceDriver
from miloco.perception.collect.miot_camera_source import (
    MiotCameraSource,
)
from miloco.perception.collect.miot_camera_source import (
    split_channel_did as split_channel_did,
)
from miloco.perception.collect.stream_buffer import (
    MultiTrackSyncBuffer,
    StreamFragment,
)
from miloco.perception.schema import (
    DecodedAudioFrame,
    DecodedVideoFrame,
    DeviceData,
)
from miloco.perception.types import PerceptionDevice

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from miloco.miot.client import MiotProxy

logger = logging.getLogger(__name__)


def _monotonic_ms() -> int:
    """Monotonic wall-clock time in milliseconds."""
    return time.monotonic_ns() // 1_000_000


def _unix_ms() -> int:
    """Unix epoch time in milliseconds."""
    return int(time.time() * 1000)


_CAMERA_TRACKS = ["decoded_video", "decoded_audio"]


@dataclass
class _CameraDeviceState:
    """Per-channel stream state — one entry per camera lens.

    Keyed by the synthetic did (``did``). For single-lens cameras that is the
    bare did (channel 0); for multi-channel cameras it carries the ``:ch{n}``
    suffix.
    """

    did: str
    sync_buffer: MultiTrackSyncBuffer = field(
        default_factory=lambda: MultiTrackSyncBuffer(_CAMERA_TRACKS)
    )
    # Clock calibration: epoch_delta = unix_ms - monotonic_ms (locked on first frame)
    # Used to convert monotonic wall_ms to unix timestamps for display.
    epoch_delta: int | None = None


class CameraDeviceAdapter(BaseDeviceAdapter):
    """Camera device type adapter — decoded video/audio frame streams."""

    device_type = "camera"
    _node_name = NodeName.CAMERA

    def __init__(
        self,
        sources: list[CameraSourceDriver] | None = None,
        on_window_ready: Callable[[], None] | None = None,
        *,
        miot_proxy: MiotProxy | None = None,
    ) -> None:
        if sources is not None and miot_proxy is not None:
            raise ValueError("Pass camera sources or miot_proxy, not both")
        self._legacy_miot_constructor = sources is None and miot_proxy is not None
        if sources is None:
            if miot_proxy is None:
                raise ValueError("At least one camera source is required")
            sources = [MiotCameraSource(miot_proxy)]
        if not sources:
            raise ValueError("At least one camera source is required")

        self._sources = list(sources)
        self._sync_lock = asyncio.Lock()
        self._miot_proxy = miot_proxy
        self._on_window_ready = on_window_ready
        self._devices: dict[str, _CameraDeviceState] = {}
        self._did_sources: dict[str, CameraSourceDriver] = {}
        self._did_source_types: dict[str, str] = {}
        self._known_devices: dict[str, PerceptionDevice] = {}

    async def discover_devices(
        self,
        all_devices: dict | None = None,
        online_only: bool = True,
        cap: bool = True,
        require_lan: bool = True,
    ) -> dict[str, PerceptionDevice]:
        merged: dict[str, PerceptionDevice] = {}
        owners: dict[str, CameraSourceDriver] = {}
        owner_types: dict[str, str] = {}
        for camera_source in self._sources:
            discovered = await camera_source.discover_devices(
                all_devices,
                online_only=online_only,
                require_lan=require_lan,
                cap=cap,
            )
            for did in sorted(discovered):
                if did in merged:
                    raise RuntimeError(
                        f"Duplicate camera DID {did!r} discovered by sources "
                        f"{owner_types[did]!r} and {camera_source.source_type!r}"
                    )
                merged[did] = discovered[did]
                owners[did] = camera_source
                owner_types[did] = camera_source.source_type

        for did in self._devices:
            if did not in owners and did in self._did_sources:
                owners[did] = self._did_sources[did]
                owner_types[did] = self._did_source_types[did]
        self._did_sources = owners
        self._did_source_types = owner_types
        self._known_devices = {
            **{
                did: device
                for did, device in self._known_devices.items()
                if did in self._devices
            },
            **merged,
        }
        return merged

    def _filter_cameras_from_all(
        self,
        all_devices: dict,
        *,
        online_only: bool = True,
        require_lan: bool = True,
        cap: bool = True,
    ) -> dict[str, PerceptionDevice]:
        """Compatibility facade for existing MIoT filtering callers."""
        for camera_source in self._sources:
            filter_cameras = getattr(camera_source, "_filter_cameras_from_all", None)
            if filter_cameras is not None:
                return filter_cameras(
                    all_devices,
                    online_only=online_only,
                    require_lan=require_lan,
                    cap=cap,
                )
        raise RuntimeError("No MIoT camera source is configured")

    async def sync_devices(self, all_devices: dict | None = None) -> None:
        """周期 sync 入口：先做「按需补建」，再走基类热插拔同步。

        登录瞬间相机 LAN 未就绪时 `refresh_cameras` 建不成 camera_img_manager，
        之后无任何机制补建 → 永久不拉流（需重启进程）。这里在周期 sync 路径
        （`all_devices is None`）检测到「scope 内应连相机数 > 已连数」时，先触发
        一次 `refresh_cameras` 补建 manager 再交基类连接。应连数用
        `online_only=True, require_lan=False`：放过 lan_online 陈旧成 false 的卡死态
        相机（要救），但排除云端就离线的相机（救不活，避免它让判据永真致 refresh
        空转）。scope 内相机要么已连、要么云端离线时不触发，零额外开销。
        """
        async with self._sync_lock:
            await self._sync_devices_unlocked(all_devices)

    async def reconcile_and_sync(
        self,
        disconnect_dids: frozenset[str],
        *,
        connect_enabled: bool,
    ) -> bool:
        """Reconcile hot-apply removals under the same lock as periodic sync."""
        async with self._sync_lock:
            success = True
            for did in sorted(disconnect_dids):
                try:
                    await self.disconnect_device(did)
                except Exception as error:  # noqa: BLE001
                    logger.error(
                        "Failed to reconcile camera %s (%s)",
                        did,
                        type(error).__name__,
                    )
                    success = False
            if connect_enabled:
                await self._sync_devices_unlocked()
            return success

    async def _sync_devices_unlocked(self, all_devices: dict | None = None) -> None:
        if all_devices is None:
            for camera_source in self._sources:
                refresh_if_needed = getattr(camera_source, "refresh_if_needed", None)
                if refresh_if_needed is None:
                    continue
                try:
                    if self._legacy_miot_constructor:
                        expected = await self.discover_devices(
                            online_only=True, require_lan=False
                        )
                        connected_count = len(self._devices)
                    else:
                        expected = await camera_source.discover_devices(
                            online_only=True, require_lan=False
                        )
                        connected_count = sum(
                            owner is camera_source
                            for did, owner in self._did_sources.items()
                            if did in self._devices
                        )
                    await refresh_if_needed(
                        expected_count=len(expected),
                        connected_count=connected_count,
                        now_ms=_monotonic_ms(),
                    )
                except Exception as error:  # noqa: BLE001
                    logger.warning("On-demand camera manager refresh failed: %s", error)
        await super().sync_devices(all_devices)

    async def connect_device(
        self, did: str, source: PerceptionDevice | None = None
    ) -> None:
        if did in self._devices:
            return

        if source is None:
            discovered = await self.discover_devices()
            if did not in discovered:
                logger.warning("Camera %s not found or offline, cannot connect", did)
                return
            source = discovered[did]

        camera_source = self._did_sources.get(did)
        if camera_source is None and len(self._sources) == 1:
            camera_source = self._sources[0]
            self._did_sources[did] = camera_source
            self._did_source_types[did] = camera_source.source_type
        if camera_source is None:
            raise RuntimeError(f"No camera source owns DID {did!r}")
        self._known_devices[did] = source

        collect_cfg = get_settings().perception.collect

        state = _CameraDeviceState(
            did=did,
            sync_buffer=MultiTrackSyncBuffer(
                track_names=_CAMERA_TRACKS,
                window_ms=collect_cfg.window_size * 1000,
                max_windows=collect_cfg.max_windows,
                on_window_ready=self._on_window_ready,
                window_settle_ms=collect_cfg.settle_ms,
                buffer_full_action=collect_cfg.full_action,
            ),
        )
        self._devices[did] = state
        try:
            await camera_source.connect_device(
                did,
                self._make_decoded_video_callback(did),
                self._make_decoded_audio_callback(did),
            )
        except Exception:
            self._devices.pop(did, None)
            state.sync_buffer.clear()
            raise
        if not camera_source.get_state(did).connected:
            self._devices.pop(did, None)
            state.sync_buffer.clear()

    async def disconnect_device(self, did: str) -> None:
        state = self._devices.pop(did, None)
        if not state:
            return

        camera_source = self._did_sources.get(did)
        if camera_source is None and len(self._sources) == 1:
            camera_source = self._sources[0]
        try:
            if camera_source is None:
                raise RuntimeError(f"No camera source owns DID {did!r}")
            await camera_source.disconnect_device(did)
        finally:
            state.sync_buffer.clear()

    async def shutdown(self) -> None:
        await super().shutdown()
        for camera_source in self._sources:
            try:
                await camera_source.shutdown()
            except Exception as error:  # noqa: BLE001
                logger.error(
                    "Failed to shutdown camera source %s (%s)",
                    camera_source.source_type,
                    type(error).__name__,
                )

    def collect(self, did: str, *, drain: bool = True) -> DeviceData | None:
        """Collect multimodal data from the device's sync buffer.

        Args:
            did: Device ID to collect from.
            drain: If True (realtime), pop the oldest ready window.
                   If False (active query), peek all buffered data.
        """
        state = self._devices.get(did)
        if not state:
            return None

        if drain:
            ready = state.sync_buffer.drain_ready()
            if ready is None or not any(ready.tracks.values()):
                return None
            # drain 后立刻拉丢包增量,clear 后给下一 cycle 重新累。
            dropped, ovf_cnt, max_depth, last_action = (
                state.sync_buffer.consume_drop_stats()
            )
            return self._build_device_data(
                state,
                ready.tracks,
                window_start_ms=ready.start_ms,
                window_end_ms=ready.end_ms,
                dropped_windows=dropped,
                overflow_count=ovf_cnt,
                max_buffer_depth=max_depth,
                last_overflow_action=last_action,
            )
        else:
            collect_ms = get_settings().perception.collect.window_size * 1000
            tracks = state.sync_buffer.peek_latest(duration_ms=collect_ms)
            if tracks is None or not any(tracks.values()):
                return None
            return self._build_device_data(state, tracks)

    def peek_latest_frame(
        self, did: str, *, window_ms: int = 2000
    ) -> "NDArray[np.uint8] | None":
        """非破坏性取该相机最近一帧解码图(numpy BGR);无缓存返 None。

        供 tier_c 闲时定期清的 live 检测用——gate 关停时正常 pipeline 不取帧,
        这里直接读 collector 已填充的 ``decoded_video`` 缓存(独立于 gate)。
        """
        state = self._devices.get(did)
        if state is None:
            return None
        tracks = state.sync_buffer.peek_latest(duration_ms=window_ms)
        if not tracks:
            return None
        dv_frags = tracks.get("decoded_video", [])
        if not dv_frags:
            return None
        return getattr(dv_frags[-1].data, "frame", None)

    @staticmethod
    def _wall_to_unix(state: _CameraDeviceState, wall_ms: int) -> int:
        """Convert monotonic wall_ms to unix_ms: unix = wall + epoch_delta."""
        if state.epoch_delta is not None:
            return wall_ms + state.epoch_delta
        return 0

    def _current_source(self, did: str) -> PerceptionDevice:
        """Build source metadata without changing the source DID identity."""
        camera_source = self._did_sources.get(did)
        if camera_source is None and len(self._sources) == 1:
            camera_source = self._sources[0]
        get_cached_device = getattr(camera_source, "get_cached_device", None)
        cached = get_cached_device(did) if get_cached_device is not None else None
        if cached is not None:
            return cached
        known = self._known_devices.get(did)
        if known is not None:
            return known
        return PerceptionDevice(did=did, name=did, device_type="camera", room_name=did)

    def _build_device_data(
        self,
        state: _CameraDeviceState,
        tracks: dict[str, list[StreamFragment]],
        window_start_ms: int = 0,
        window_end_ms: int = 0,
        *,
        dropped_windows: int = 0,
        overflow_count: int = 0,
        max_buffer_depth: int = 0,
        last_overflow_action: str | None = None,
    ) -> DeviceData | None:
        """Build DeviceData from decoded frame track fragments.

        Additionally aggregates per-frame ``decode_latency_ms`` into
        per-window averages (video / audio / combined).  This is the
        packaging point — downstream consumers (collector, pipeline)
        read the precomputed aggregates rather than re-walking frames.
        """
        dv_frags = tracks.get("decoded_video", [])
        da_frags = tracks.get("decoded_audio", [])

        if not dv_frags and not da_frags:
            return None

        video = [f.data for f in dv_frags]
        audio = [f.data for f in da_frags]

        v_count = len(video)
        a_count = len(audio)
        total_frames = v_count + a_count

        def _avg(sum_: float, count: int) -> float:
            return (sum_ / count) if count else 0.0

        # Decode-latency aggregates.
        v_decode_sum = sum(f.decode_latency_ms for f in video)
        a_decode_sum = sum(f.decode_latency_ms for f in audio)
        decode_video_avg = _avg(v_decode_sum, v_count)
        decode_audio_avg = _avg(a_decode_sum, a_count)
        decode_combined = _avg(v_decode_sum + a_decode_sum, total_frames)

        return DeviceData(
            meta=self._current_source(state.did),
            video=video,
            audio=audio,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
            window_start_unix_ms=self._wall_to_unix(state, window_start_ms),
            window_end_unix_ms=self._wall_to_unix(state, window_end_ms),
            decode_avg_ms=decode_combined,
            decode_video_avg_ms=decode_video_avg,
            decode_audio_avg_ms=decode_audio_avg,
            dropped_windows=dropped_windows,
            overflow_count=overflow_count,
            max_buffer_depth=max_buffer_depth,
            last_overflow_action=last_overflow_action,
        )

    def get_connected_devices(self) -> dict[str, PerceptionDevice]:
        return {did: self._current_source(did) for did in self._devices}

    def clear_buffers(self) -> None:
        """Clear all camera sync buffers without disconnecting devices."""
        for did, state in self._devices.items():
            state.sync_buffer.clear()
            logger.info("Cleared sync buffer for camera %s", did)

    # ---- Callback factories ----

    @staticmethod
    def _calibrate(state: _CameraDeviceState, stream_ts: int) -> tuple[int, int]:
        """Return (wall_ms, unix_ms) for a frame.

        wall_ms is the actual system monotonic time (immune to stream clock
        drift).  epoch_delta (unix - mono) is locked on first call and used
        to derive unix_ms for display.
        """
        wall_ms = _monotonic_ms()
        if state.epoch_delta is None:
            state.epoch_delta = _unix_ms() - wall_ms
            logger.debug(
                "Clock calibrated for %s: epoch_delta=%d ms",
                state.did,
                state.epoch_delta,
            )
        unix_ms = wall_ms + state.epoch_delta
        return wall_ms, unix_ms

    @staticmethod
    def _compute_decode_latency(
        recv_unix_ms: int,
        decoded_unix_ms: int,
    ) -> float:
        """Compute per-frame ``decode_latency_ms = decoded - recv``.

        Both timestamps are stamped host-locally inside the MIoT SDK
        (``recv_unix_ms`` in ``miot.camera.__on_raw_data`` before
        enqueue, ``decoded_unix_ms`` right after ``av.decode()`` returns
        in ``miot.decoder``), so the delta is a clean host-local measure
        of "queue + FFmpeg decode" with no cross-clock assumptions.

        Guards:
        * ``recv_unix_ms == 0`` means the frame pre-dates the
          instrumented path (e.g. tests or legacy callbacks) — returns
          ``0.0`` to signal "unknown".
        * Negative values (clock skew, reconnect artifacts) are clamped
          to ``0.0``.
        """
        if recv_unix_ms == 0:
            return 0.0
        decode_ms = float(decoded_unix_ms - recv_unix_ms)
        if decode_ms < 0:
            decode_ms = 0.0
        return decode_ms

    def _make_decoded_video_callback(self, did: str):
        """Decoded video frame callback: feeds decoded_video track in sync buffer.

        Receives BGR numpy arrays (already converted from PyAV in decoder thread).
        """

        async def _on_decoded_video(
            did_: str,
            frame: NDArray[np.uint8],
            ts: int,
            ch: int,
            recv_unix_ms: int = 0,
            decoded_unix_ms: int = 0,
        ):
            async with get_monitor().track_async(NodeName.CAMERA, "decode_video") as h:
                state = self._devices.get(did)
                if not state:
                    # 设备已断开但回调仍在排队的 race: 不计入 fps_60s,
                    # 避免 stale 回调虚高 SOURCE 节点的处理速率指标。
                    h.skip_rolling()
                    return
                wall_ms, unix_ms = self._calibrate(state, ts)
                decode_latency_ms = self._compute_decode_latency(
                    recv_unix_ms, decoded_unix_ms
                )
                decoded = DecodedVideoFrame(
                    frame=frame,
                    stream_ts=ts,
                    wall_ms=wall_ms,
                    unix_ms=unix_ms,
                    recv_unix_ms=recv_unix_ms,
                    decoded_unix_ms=decoded_unix_ms,
                    decode_latency_ms=decode_latency_ms,
                )
                state.sync_buffer.put(
                    "decoded_video", decoded, stream_ts=ts, wall_ms=wall_ms
                )

        return _on_decoded_video

    def _make_decoded_audio_callback(self, did: str):
        """Decoded audio frame callback: feeds decoded_audio track in sync buffer.

        Receives PCM numpy arrays (already resampled from PyAV in decoder thread).
        """

        async def _on_decoded_audio(
            did_: str,
            frame: NDArray[np.int16],
            ts: int,
            ch: int,
            recv_unix_ms: int = 0,
            decoded_unix_ms: int = 0,
        ):
            async with get_monitor().track_async(NodeName.CAMERA, "decode_audio") as h:
                state = self._devices.get(did)
                if not state:
                    # 设备已断开但回调仍在排队的 race: 不计入 fps_60s,
                    # 避免 stale 回调虚高 SOURCE 节点的处理速率指标。
                    h.skip_rolling()
                    return
                wall_ms, unix_ms = self._calibrate(state, ts)
                decode_latency_ms = self._compute_decode_latency(
                    recv_unix_ms, decoded_unix_ms
                )
                decoded = DecodedAudioFrame(
                    frame=frame,
                    stream_ts=ts,
                    wall_ms=wall_ms,
                    unix_ms=unix_ms,
                    recv_unix_ms=recv_unix_ms,
                    decoded_unix_ms=decoded_unix_ms,
                    decode_latency_ms=decode_latency_ms,
                )
                state.sync_buffer.put(
                    "decoded_audio", decoded, stream_ts=ts, wall_ms=wall_ms
                )

        return _on_decoded_audio
