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
import inspect
import logging
import math
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, cast

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
    TrackRetentionPolicy,
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
_RTSP_VIDEO_BUFFER_BYTES = 128 * 1024 * 1024

# 按需补建 refresh_cameras 的最小间隔：无设备态下 sync 循环 1s 一轮，
# 不节流会变成每秒一次重 SDK 调用 + 建连尝试。10s 足够让相机就绪后及时恢复。
_ONDEMAND_REFRESH_MIN_INTERVAL_MS = 10_000

# 静默检测：感知视频流 N 秒无帧 → 判僵尸连接。miss 层对「连接在但不出帧」的静默
# 无解（keepalive 只探连接活性、不探数据流），只能上层检测 + destroy/create 重拉。
# 正常流 ~1fps，30s 无帧基本确定是断；再短会误伤正常低帧。
_SILENCE_THRESHOLD_MS = 30_000

# 首帧专用上界：原生建连 + 首个 IDR 最慢约 15s，留足余量。超过它仍一帧未到 →
# 与「出过帧后静默」同等对待，走 destroy+create 自愈。没有这个上界，
# last_video_frame_ms 恒为 0 的通道（原生会话建起来了但媒体流一帧不来，正是跨网段 /
# 严格 NAT 最典型的僵尸态）会被「等首帧」分支无条件跳过 → 故障越彻底越救不回来。
_FIRST_FRAME_THRESHOLD_MS = 90_000

# 重连防抖：同台相机重连后 N 秒内不再重连，避免真坏相机 30s 一轮空转。
_RECONNECT_COOLDOWN_MS = 5 * 60_000



def _normalize_perception_fps(raw: object) -> int:
    """Return a safe, positive perception frame rate."""
    try:
        return max(1, int(cast(Any, raw)))
    except (TypeError, ValueError, OverflowError):
        return 1


def _perception_input_fps() -> int:
    raw = get_settings().perception.engine.get("input", {}).get("fps", 3)
    return _normalize_perception_fps(raw)


@dataclass
class _CameraDeviceState:
    """Per-channel stream state — one entry per camera lens.

    Keyed by the synthetic did (``did``). For single-lens cameras that is the
    bare did (channel 0); for multi-channel cameras it carries the ``:ch{n}``
    suffix.
    """

    did: str
    source_generation: int = field(default_factory=time.monotonic_ns)
    sync_buffer: MultiTrackSyncBuffer = field(
        default_factory=lambda: MultiTrackSyncBuffer(_CAMERA_TRACKS)
    )
    # Clock calibration: epoch_delta = unix_ms - monotonic_ms (locked on first frame)
    # Used to convert monotonic wall_ms to unix timestamps for display.
    epoch_delta: int | None = None
    rtsp_target_fps: int | None = None
    last_rtsp_video_admit_ms: int | None = None
    rtsp_admitted_frames: int = 0
    rtsp_dropped_frames: int = 0
    # 最近一帧视频的 monotonic wall_ms，静默检测用。
    last_video_frame_ms: int = 0
    # 订阅完成时刻的 monotonic wall_ms。首帧未到（last_video_frame_ms == 0）时
    # 替代它参与静默判定，给「等首帧」一个上界，见 _FIRST_FRAME_THRESHOLD_MS。
    connected_at_ms: int = 0


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
        perception_fps_provider: Callable[[], int] = _perception_input_fps,
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
        self._perception_fps_provider = perception_fps_provider
        self._devices: dict[str, _CameraDeviceState] = {}
        self._did_sources: dict[str, CameraSourceDriver] = {}
        self._did_source_types: dict[str, str] = {}
        self._known_devices: dict[str, PerceptionDevice] = {}
        self._last_ondemand_refresh_ms = 0
        # 静默重连防抖标记：did -> 最近一次重连的 monotonic ms。
        self._last_reconnect_ms: dict[str, int] = {}

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
            if camera_source.source_type == "miot" and any(
                not device.online for device in discovered.values()
            ):
                proxy = getattr(camera_source, "_miot_proxy", None)
                cameras = (
                    all_devices
                    if all_devices
                    else await proxy.get_cameras() if proxy is not None else {}
                )
                for did, device in discovered.items():
                    physical_did, _ = split_channel_did(did)
                    camera = cameras.get(physical_did)
                    if (
                        camera is not None
                        and getattr(camera, "online", False)
                        and getattr(camera, "connected", False)
                    ):
                        device.online = True
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

    async def sync_devices(
        self,
        all_devices: dict | None = None,
        disconnect_require_lan: bool = False,
    ) -> None:
        """周期 sync 入口：先做「按需补建」，再走基类热插拔同步。

        登录瞬间相机 LAN 未就绪时 `refresh_cameras` 建不成 camera_img_manager，
        之后无任何机制补建 → 永久不拉流（需重启进程）。这里在周期 sync 路径
        （`all_devices is None`）检测到「scope 内应连**集合**里还有成员没连上」时，
        先触发一次 `refresh_cameras` 补建 manager 再交基类连接。

        两条判据都别照直觉改，理由都写在下方注释里：

        - 应连集合用**严格门**（`online_only=True`，`require_lan` 保持默认 True），
          与 `refresh_cameras` 建销 manager 的 `select_active_camera_dids` 完全同口径
          —— 补建问的是「refresh_cameras 会不会真为它建 manager」，用宽松门只会让判据
          永真、每轮空转打一次云端接口。
        - 判据取**集合差**而非数量比较：数量看不见「数量相等、成员不同」。

        scope 内相机全部已连时不触发，零额外开销。

        ``disconnect_require_lan`` 默认 **False**，与基类的 True 刻意不同：相机的
        断开判据走宽松门，理由见下方 super 调用处的注释。注意它**不是签名占位**
        ——取值原样透传给基类（见下方 ``super().sync_devices``），显式传 True 会
        把断开判据切回严格门，保留集就不再是发现集的超集（超上限家庭里唯一可达
        的相机会每轮连上、下一轮又被断，永不自愈）。当前没有任何调用方传它，
        加调用点前先读一遍那条不变量。（兄弟方法 ``discover_devices`` 的
        ``online_only`` / ``require_lan`` / ``cap`` 同理也是透传生效的——基类
        重算保留集用的 ``require_lan=False, cap=False`` 正是这条不变量的实现。）
        """
        async with self._sync_lock:
            await self._sync_devices_unlocked(all_devices, disconnect_require_lan)

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
            else:
                pruned = await self._prune_inactive_pending_devices()
                self._remove_pruned_devices(pruned)
            return success

    async def _sync_devices_unlocked(
        self, all_devices: dict | None = None, disconnect_require_lan: bool = False
    ) -> None:
        pruned = await self._prune_inactive_pending_devices()
        try:
            if all_devices is None:
                await self._check_stalled_cameras()
                recovery_retry_dids = await self._advance_auto_recovery(_monotonic_ms())
                if recovery_retry_dids:
                    retry_pruned = {
                        did: state
                        for did, state in pruned.items()
                        if did in recovery_retry_dids
                    }
                    if retry_pruned:
                        self._remove_pruned_devices(retry_pruned)
                        for did in retry_pruned:
                            pruned.pop(did, None)
                for camera_source in self._sources:
                    refresh_if_needed = getattr(
                        camera_source, "refresh_if_needed", None
                    )
                    if refresh_if_needed is None and not self._legacy_miot_constructor:
                        continue
                    try:
                        if self._legacy_miot_constructor:
                            expected = await self.discover_devices(online_only=True)
                            missing = set(expected) - self._devices.keys()
                            now_ms = _monotonic_ms()
                            if (
                                missing
                                and self._miot_proxy.is_authenticated
                                and now_ms - self._last_ondemand_refresh_ms
                                >= _ONDEMAND_REFRESH_MIN_INTERVAL_MS
                            ):
                                self._last_ondemand_refresh_ms = now_ms
                                await self._miot_proxy.refresh_cameras()
                        else:
                            expected = await camera_source.discover_devices(
                                online_only=True
                            )
                            connected_count = len(expected) - len(
                                set(expected) - self._devices.keys()
                            )
                            await refresh_if_needed(
                                expected_count=len(expected),
                                connected_count=connected_count,
                                now_ms=_monotonic_ms(),
                            )
                    except Exception as error:  # noqa: BLE001
                        logger.warning(
                            "On-demand camera manager refresh failed: %s", error
                        )
            await super().sync_devices(
                all_devices, disconnect_require_lan=disconnect_require_lan
            )
            await self._converge_feed_cap(all_devices)
        finally:
            self._remove_pruned_devices(pruned)

    async def _advance_auto_recovery(self, now_ms: int) -> frozenset[str]:
        retry_dids: set[str] = set()
        disabled_count = 0
        for camera_source in self._sources:
            advance = getattr(camera_source, "advance_auto_recovery", None)
            if not callable(advance):
                continue
            try:
                result = advance(now_ms=now_ms)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as error:  # noqa: BLE001
                logger.warning(
                    "RTSP auto-recovery failed for source %s (%s)",
                    camera_source.source_type,
                    type(error).__name__,
                )
                continue
            if getattr(result, "success", True) is not True:
                logger.warning("RTSP auto-recovery reported incomplete source mutation")
            result_retry_dids = getattr(result, "retry_dids", frozenset())
            result_disabled_dids = getattr(result, "disabled_dids", frozenset())
            try:
                retry_dids.update(str(did) for did in result_retry_dids)
                disabled_count += len(result_disabled_dids)
            except TypeError:
                logger.warning("RTSP auto-recovery returned invalid result shape")
        if retry_dids or disabled_count:
            logger.info(
                "RTSP auto-recovery advanced: retry=%d disabled=%d",
                len(retry_dids),
                disabled_count,
            )
        return frozenset(retry_dids)

    async def _converge_feed_cap(self, all_devices: dict | None = None) -> None:
        """把超出投喂上限的通道断掉,口径与 select_active_camera_dids 完全一致。

        为什么上限收敛不能寄生在基类的断开判据里:那个「保留集」为了满足
        「保留集 ⊇ 发现集」的不变量必须 ``cap=False``（截断按合成 did 升序取前 N,
        ``sorted(超集)[:N]`` 不含 ``sorted(子集)[:N]``），于是它再也收不住已连集的
        规模;而连接侧走的是带截断的发现集、``connect_device`` 自己不认上限。两边
        一叠加:低字典序相机上线被发现集纳入并新连,先前占位的高字典序相机仍在保留
        集里不被断开 ⇒ 已连路数单调越过上限,且被挤出活跃集的那路会在
        ``refresh_cameras``(销 manager) 与静默检测(建 manager) 之间无限震荡,白占
        相机有限的并发流名额。所以上限收敛独立收在这里,基类对投喂上限保持无感知。

        淘汰顺序先看「本轮还通过严格门吗」、再看字典序。只按字典序排会把优先级反转:
        保留集刻意放宽了 LAN 门（正是本 PR 要救的那类:云端在线但 LAN 已探不到、原生也
        没连上），这种僵尸通道若字典序靠前,就会挤掉字典序靠后、刚刚真连上的健康通道 ——
        日志每轮刷一条 over feed cap、那一路的投喂反复中断,而占着名额的僵尸一帧不出。
        （该链有兜底:僵尸静默满 30s / 首帧满 90s 后被静默检测连带断开、名额释放,所以
        表现是「另一台掉线后的 30~90s 窗口内健康相机被反复断连 3~9 次」而非永不自愈,
        但优先级反转本身与本方法要消灭的抖动是同一类失败模式。）
        """
        from miloco.miot.filter import MAX_ENABLED_CAMERAS

        miot_dids = [
            did for did in self._devices
            if (
                self._did_sources.get(did)
                or (self._sources[0] if len(self._sources) == 1 else None)
            ) is not None
            and (
                self._did_sources.get(did)
                or self._sources[0]
            ).source_type == "miot"
        ]
        if len(miot_dids) <= MAX_ENABLED_CAMERAS:
            # 未超限直接退,省掉下面那次 discover(常态路径零开销)。
            return
        try:
            # 严格门 + 截断,与 refresh_cameras 的 manager 建销同口径;传入本轮的
            # all_devices 快照,热插拔路径不另取一份可能已漂移的相机表。
            preferred = set(await self.discover_devices(all_devices))
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Feed-cap converge discover failed (%s); falling back to did order", e
            )
            preferred = set()
        ordered = sorted(miot_dids, key=lambda d: (d not in preferred, d))
        for did in ordered[MAX_ENABLED_CAMERAS:]:
            logger.warning(
                "Camera %s over feed cap (%d), disconnecting overflow channel",
                did,
                MAX_ENABLED_CAMERAS,
            )
            try:
                await self.disconnect_device(did)
            except Exception as e:  # noqa: BLE001
                logger.error("Overflow disconnect failed %s: %s", did, e)

    async def _check_stalled_cameras(self) -> None:
        """静默检测：感知视频流超阈值无帧 → 判僵尸连接并触发重连。

        miss 层对「连接在但不出帧」的静默无解（keepalive 只探连接活性、不探数据流），
        只能上层检测 + 主动 destroy/create 重拉。只在周期 sync 路径跑，避免热插拔
        语义被静默检测打断。
        """
        now_ms = _monotonic_ms()
        # 先按物理 did 归并本轮所有静默通道：多镜头相机的 ch0/ch1 共用同一个 native
        # 会话，一次 destroy+create 就够；按通道 did 各触发一次等于重复重建（重建的
        # 还是同一个物理会话，几毫秒内 destroy 两次，四镜头就是四次）。
        stalled_by_physical: dict[str, list[str]] = {}
        for did, state in list(self._devices.items()):
            camera_source = self._did_sources.get(did)
            if camera_source is None and len(self._sources) == 1:
                camera_source = self._sources[0]
            if camera_source is None or camera_source.source_type != "miot":
                continue
            # 首帧未到 → 用「订阅时刻」判，阈值放宽到 _FIRST_FRAME_THRESHOLD_MS
            # （连接刚建立确实要等十几秒）；首帧已到 → 用「最后一帧时刻」判，
            # 阈值 _SILENCE_THRESHOLD_MS。
            # 这里必须有上界:last_video_frame_ms 只有帧到达才脱离 0，若无条件跳过，
            # 「一帧都没出」这个最坏的僵尸态会永久免疫检测（连日志都不留）。
            if state.last_video_frame_ms == 0:
                if (
                    state.connected_at_ms == 0
                    or now_ms - state.connected_at_ms < _FIRST_FRAME_THRESHOLD_MS
                ):
                    continue
            elif now_ms - state.last_video_frame_ms < _SILENCE_THRESHOLD_MS:
                continue
            physical_did, _ = split_channel_did(did)
            proxy = getattr(camera_source, "_miot_proxy", None)
            cam = proxy.get_cached_camera(physical_did) if proxy is not None else None
            # 云端已离线 → 救不活，交给基类按在线态断开，别白重连。
            if cam is not None and not cam.online:
                continue
            # 带上判定依据:「从未出帧」多半是路由/NAT 建不起媒体流,「出过帧后静默」
            # 多半是相机侧打嗝——两者运维处置不同,日志里要能一眼分开。
            stalled_by_physical.setdefault(physical_did, []).append(
                f"{did}(no-first-frame in {now_ms - state.connected_at_ms}ms)"
                if state.last_video_frame_ms == 0
                else f"{did}(silent {now_ms - state.last_video_frame_ms}ms)"
            )

        for physical_did, stalled_dids in stalled_by_physical.items():
            # 防抖按物理 did 计：同一台相机 5min 内只重建一次。
            if (
                now_ms - self._last_reconnect_ms.get(physical_did, 0)
                < _RECONNECT_COOLDOWN_MS
            ):
                continue
            logger.warning(
                "Camera %s stalled (channels=%s), reconnecting",
                physical_did,
                stalled_dids,
            )
            await self._reconnect_stalled(physical_did)

    async def _reconnect_stalled(self, physical_did: str) -> None:
        """重建一台静默相机：停该相机全部通道的解码订阅 → 重建 native 会话。

        三层重建缺一不可：disconnect_device 只 unregister 解码回调（不动 native miss
        会话）；reconnect_camera 才 destroy+create manager 真正断掉僵尸 MTP/PPCS 会话、
        重走 miss_client_connect 的建连重试；解码订阅由**同一轮** sync 紧随其后的
        connect_device 补齐（``sync_devices`` 先跑静默检测再跑基类同步，本方法已把这些
        通道从 ``_devices`` 摘掉，它们当轮就落进「发现集 − 已连集」）。
        必须把同一物理相机的所有已连通道一起断开：destroy 会连带作废兄弟通道在旧
        实例上的 reg_id，而 connect_device 对已在 _devices 里的 did 直接 early-return，
        不先断开就永远补不回订阅。
        """
        self._last_reconnect_ms[physical_did] = _monotonic_ms()
        miot_source = next(
            (source for source in self._sources if source.source_type == "miot"), None
        )
        proxy = getattr(miot_source, "_miot_proxy", None)
        if proxy is None:
            return
        siblings = [
            d for d in list(self._devices)
            if split_channel_did(d)[0] == physical_did
            and (
                self._did_sources.get(d)
                or (self._sources[0] if len(self._sources) == 1 else None)
            ) is miot_source
        ]
        for d in siblings:
            try:
                await self.disconnect_device(d)
            except Exception as e:  # noqa: BLE001
                logger.error("Stalled camera disconnect failed %s: %s", d, e)
        try:
            await proxy.reconnect_camera(physical_did)
        except Exception as e:  # noqa: BLE001
            logger.error(
                "Stalled camera reconnect failed %s: %s", physical_did, e
            )
            return
        # 感知侧的解码订阅由同一轮 sync 的基类同步补齐，但 watch 直播 / record_clip /
        # 播放页音频的订阅也一样死在被 destroy 的旧实例上，且它们没有 sync 这条兜底
        # （见两个 manager 的 resubscribe_camera 说明）。局部 import 防循环依赖，也避免
        # client → ws 的反向依赖。视频与音频各自独立 try：一条失败不该挡住另一条。
        try:
            from miloco.miot.ws import miot_video_stream_manager

            await miot_video_stream_manager.resubscribe_camera(physical_did)
        except Exception as e:  # noqa: BLE001
            logger.error(
                "Resubscribe live/record streams failed %s: %s", physical_did, e
            )
        try:
            from miloco.miot.ws import miot_audio_stream_manager

            await miot_audio_stream_manager.resubscribe_camera(physical_did)
        except Exception as e:  # noqa: BLE001
            logger.error("Resubscribe audio streams failed %s: %s", physical_did, e)

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
        rtsp_target_fps: int | None = None
        retention_policy: TrackRetentionPolicy | None = None
        if camera_source.source_type == "rtsp":
            rtsp_target_fps = _normalize_perception_fps(self._perception_fps_provider())
            retention_policy = TrackRetentionPolicy(
                track="decoded_video",
                max_items_per_window=math.ceil(
                    rtsp_target_fps * collect_cfg.window_size
                )
                + 1,
                max_payload_bytes=_RTSP_VIDEO_BUFFER_BYTES,
                payload_size=lambda item: cast(DecodedVideoFrame, item).frame.nbytes,
            )

        state = _CameraDeviceState(
            did=did,
            sync_buffer=MultiTrackSyncBuffer(
                track_names=_CAMERA_TRACKS,
                window_ms=collect_cfg.window_size * 1000,
                max_windows=collect_cfg.max_windows,
                on_window_ready=self._on_window_ready,
                window_settle_ms=collect_cfg.settle_ms,
                buffer_full_action=collect_cfg.full_action,
                retention_policy=retention_policy,
            ),
            rtsp_target_fps=rtsp_target_fps,
        )
        self._devices[did] = state
        try:
            await camera_source.connect_device(
                did,
                self._make_decoded_video_callback(did, state),
                self._make_decoded_audio_callback(did, state),
            )
        except Exception:
            self._devices.pop(did, None)
            state.sync_buffer.clear()
            raise
        source_state = camera_source.get_state(did)
        pending_registered = False
        if not source_state.connected:
            _, pending_registered = self._pending_registration_decision(
                camera_source, did
            )
        if source_state.connected and isinstance(camera_source, MiotCameraSource):
            state.connected_at_ms = _monotonic_ms()
        if not source_state.connected and not pending_registered:
            self._devices.pop(did, None)
            state.sync_buffer.clear()
            await self._disconnect_source_registration(camera_source, did)

    def _pending_registration_decision(
        self,
        camera_source: CameraSourceDriver,
        did: str,
    ) -> tuple[bool, bool]:
        try:
            retain_pending = getattr(camera_source, "retain_pending_connection", None)
            if not callable(retain_pending):
                return False, False
            result = retain_pending(did)
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "Pending camera registration check failed for %s %s (%s)",
                camera_source.source_type,
                did,
                type(error).__name__,
            )
            return True, False

        if inspect.iscoroutine(result):
            result.close()
        if result is True:
            return True, True
        if result is not False:
            logger.warning(
                "Pending camera registration check returned invalid type for %s %s (%s)",
                camera_source.source_type,
                did,
                type(result).__name__,
            )
        return True, False

    async def _prune_inactive_pending_devices(
        self,
    ) -> dict[str, _CameraDeviceState]:
        pruned: dict[str, _CameraDeviceState] = {}
        for did, state in list(self._devices.items()):
            camera_source = self._did_sources.get(did)
            if camera_source is None:
                continue
            source_state = camera_source.get_state(did)
            if source_state.connected:
                continue
            has_capability, retained = self._pending_registration_decision(
                camera_source, did
            )
            if not has_capability or retained:
                continue
            await self._disconnect_source_registration(camera_source, did)
            state.sync_buffer.clear()
            pruned[did] = state
        return pruned

    def _remove_pruned_devices(self, pruned: dict[str, _CameraDeviceState]) -> None:
        for did, state in pruned.items():
            if self._devices.get(did) is state:
                self._devices.pop(did, None)
                state.sync_buffer.clear()

    @staticmethod
    async def _disconnect_source_registration(
        camera_source: CameraSourceDriver,
        did: str,
    ) -> None:
        try:
            await camera_source.disconnect_device(did)
        except Exception as error:  # noqa: BLE001
            logger.error(
                "Failed to clear camera registration for %s %s (%s)",
                camera_source.source_type,
                did,
                type(error).__name__,
            )

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

    def collect(self, did: str, *, drain: bool = True, fifo: bool = False) -> DeviceData | None:
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
            ready = state.sync_buffer.drain_ready(mode="fifo" if fifo else "latest")
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
                partial_windows_count=state.sync_buffer.consume_partial_stats(),
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
            if camera_source is not None and camera_source.source_type == "miot":
                proxy = getattr(camera_source, "_miot_proxy", None)
                physical_did, _ = split_channel_did(did)
                camera = proxy.get_cached_camera(physical_did) if proxy is not None else None
                if (
                    camera is not None
                    and getattr(camera, "online", False)
                    and getattr(camera, "connected", False)
                ):
                    cached.online = True
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
        partial_windows_count: int = 0,
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
            partial_windows_count=partial_windows_count,
            source_generation=(state.source_generation << 16) + state.sync_buffer.generation,
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

    @staticmethod
    def _admit_rtsp_video(state: _CameraDeviceState, now_ms: int) -> bool:
        """Apply one RTSP device's host-monotonic perception cadence."""
        target_fps = state.rtsp_target_fps
        if target_fps is None:
            return True
        last = state.last_rtsp_video_admit_ms
        if last is not None and now_ms - last < 1000 / target_fps:
            state.rtsp_dropped_frames += 1
            return False
        state.last_rtsp_video_admit_ms = now_ms
        state.rtsp_admitted_frames += 1
        return True

    def _make_decoded_video_callback(self, did: str, state: _CameraDeviceState):
        """Decoded video frame callback: feeds decoded_video track in sync buffer.

        Receives BGR numpy arrays (already converted from PyAV in decoder thread).

        ``state`` 是回调订阅时刻绑定的设备状态对象。回调只向**这个** state 的
        buffer 写帧：若 ``self._devices[did]`` 已不是它（静默自愈重连换了新
        状态），说明帧来自已失效的流，直接丢弃。这是对 disconnect→reconnect
        竞态的根本防护——unregister 后原生解码线程仍可能有在途帧 dispatch，
        若只按「did 有无 state」判活，旧流的在途帧会混进新 buffer。
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
                current = self._devices.get(did)
                if current is not state:
                    # state 已被替换/移除: 帧来自已失效的流。丢弃且不计入 fps_60s,
                    # 避免 stale 回调虚高 SOURCE 节点的处理速率指标。
                    h.skip_rolling()
                    return
                wall_ms, unix_ms = self._calibrate(state, ts)
                if not self._admit_rtsp_video(state, wall_ms):
                    h.skip_rolling()
                    return
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
                state.last_video_frame_ms = wall_ms
                state.sync_buffer.put(
                    "decoded_video", decoded, stream_ts=ts, wall_ms=wall_ms
                )

        return _on_decoded_video

    def _make_decoded_audio_callback(self, did: str, state: _CameraDeviceState):
        """Decoded audio frame callback: feeds decoded_audio track in sync buffer.

        Receives PCM numpy arrays (already resampled from PyAV in decoder thread).

        ``state`` 语义同 video 回调: 只向订阅时刻绑定的 state 写帧,state 被替换
        (disconnect→重连) 后丢弃,防 stale 音频帧混入新 buffer。
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
                current = self._devices.get(did)
                if current is not state:
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
