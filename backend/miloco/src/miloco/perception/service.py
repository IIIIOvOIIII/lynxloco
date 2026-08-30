"""
Perception service layer.

Orchestrates the realtime engine, active perception queries,
perception log retrieval, and device management.

Active perception uses the same pipeline as realtime — data is collected
from the realtime stream buffers via collector.collect_batch(),
ensuring a unified data path.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from miloco.database.on_demand_log_repo import OnDemandLogRepo
from miloco.database.perception_repo import PerceptionLogRepo
from miloco.middleware.exceptions import BusinessException
from miloco.perception.collect.collector import MultimodalCollector
from miloco.perception.collect.rtsp_camera_source import RtspApplyResult
from miloco.perception.processor import PipelineProcessor
from miloco.perception.runner import PerceptionRunner
from miloco.perception.schema import (
    OnDemandPerceptionRequest,
    OnDemandPerceptionResultItem,
    PerceptionEngineStatus,
    PerceptionRuntimeSummary,
    RuntimeEngineSummary,
    RuntimeLogSummary,
    RuntimeOmniSummary,
    RuntimeSourceSummary,
    RuntimeWindowSummary,
)
from miloco.perception.types import PerceptionDevice
from miloco.utils.time_utils import ms_to_iso_local, now_ms


class _RtspSettingsApplier(Protocol):
    async def apply_settings(self) -> RtspApplyResult: ...

    async def request_retry(self, did: str) -> bool: ...


class _CameraSourceSynchronizer(Protocol):
    async def reconcile_and_sync(
        self,
        disconnect_dids: frozenset[str],
        *,
        connect_enabled: bool,
    ) -> bool: ...


logger = logging.getLogger(__name__)


def _zero_observability_window(minutes: int) -> dict[str, int]:
    return {
        "minutes": minutes,
        "cycle_count": 0,
        "skipped_count": 0,
        "video_pass_count": 0,
        "audio_pass_count": 0,
        "hold_pass_count": 0,
        "omni_call_count": 0,
        "omni_error_count": 0,
        "cycle_error_count": 0,
        "dropped_windows_count": 0,
        "overflow_count": 0,
    }


def classify_perception_runtime_state(
    *,
    running: bool,
    ready: bool,
    active_source_count: int,
    raw_last_hour: int,
    meaningful_last_hour: int,
    consecutive_empty_descriptions: int,
    recent_window: Mapping[str, int],
) -> tuple[str, list[str]]:
    if not running:
        return "inactive", ["engine_stopped"]
    if not ready:
        return "not_ready", ["engine_not_ready"]
    if active_source_count <= 0:
        return "no_sources", ["no_active_sources"]

    cycle_count = int(recent_window.get("cycle_count", 0))
    omni_call_count = int(recent_window.get("omni_call_count", 0))
    omni_error_count = int(recent_window.get("omni_error_count", 0))
    cycle_error_count = int(recent_window.get("cycle_error_count", 0))
    error_rate = omni_error_count / max(omni_call_count, 1)

    if cycle_error_count > 0 or error_rate >= 0.25:
        return "degraded", ["high_error_rate"]
    if meaningful_last_hour > 0:
        return "eventing", ["meaningful_events_recent"]
    if raw_last_hour > 0:
        return "describing", ["raw_descriptions_recent"]
    if cycle_count > 0 and omni_call_count > 0 and consecutive_empty_descriptions >= 3:
        return (
            "silent",
            [
                "engine_active",
                "recent_omni_calls",
                "semantic_output_empty",
                "raw_logs_deduplicated",
            ],
        )
    return "collecting", ["engine_active"]


class PerceptionService:
    """Service for all perception operations."""

    def __init__(
        self,
        collector: MultimodalCollector,
        pipeline: PipelineProcessor,
        perception_runner: PerceptionRunner,
        log_repo: PerceptionLogRepo,
        on_demand_log_repo: OnDemandLogRepo | None = None,
        meaningful_events_dao: object | None = None,
        rtsp_camera_source: _RtspSettingsApplier | None = None,
        camera_adapter: _CameraSourceSynchronizer | None = None,
    ):
        self._collector = collector
        self._pipeline = pipeline
        self._engine = perception_runner
        self._log_repo = log_repo
        self._od_log_repo = on_demand_log_repo or OnDemandLogRepo()
        self._meaningful_events_dao = meaningful_events_dao
        self._rtsp_camera_source = rtsp_camera_source
        self._camera_adapter = camera_adapter
        # 串行化引擎生命周期操作(start/stop/重建/降级)。这些操作都含多个 await
        # 让出点且改 runner._is_running,不加锁会在「应用设置重启」与用户手动
        # 启停/删模型交错时出现 executor 未重挂、孤儿 task 等状态错乱。
        self._lifecycle_lock = asyncio.Lock()
        self._camera_sources_lock = asyncio.Lock()

    # ---- Realtime engine lifecycle ----

    async def start_engine(self) -> None:
        async with self._lifecycle_lock:
            await self._engine.start()

    async def stop_engine(self) -> None:
        async with self._lifecycle_lock:
            await self._engine.stop()

    async def sync_camera_sources(self) -> bool:
        """Atomically apply RTSP settings and reconcile the unified adapter."""
        if self._rtsp_camera_source is None or self._camera_adapter is None:
            return True
        async with self._lifecycle_lock:
            async with self._camera_sources_lock:
                result = await self._rtsp_camera_source.apply_settings()
                success = result.success
                try:
                    reconciled = await self._camera_adapter.reconcile_and_sync(
                        result.reconcile_dids,
                        connect_enabled=self._engine.is_running,
                    )
                    success = success and reconciled
                except Exception as error:  # noqa: BLE001
                    logger.error(
                        "[service] camera source sync failed (%s)",
                        type(error).__name__,
                    )
                    success = False
                return success

    async def retry_camera_source(self, did: str) -> bool:
        """Clear one terminal tombstone and perform one explicit reconnect."""
        if self._rtsp_camera_source is None or self._camera_adapter is None:
            return True
        async with self._lifecycle_lock:
            async with self._camera_sources_lock:
                if not await self._rtsp_camera_source.request_retry(did):
                    return False
                try:
                    return await self._camera_adapter.reconcile_and_sync(
                        frozenset({did}),
                        connect_enabled=self._engine.is_running,
                    )
                except Exception as error:  # noqa: BLE001
                    logger.error(
                        "[service] camera source retry failed (%s)",
                        type(error).__name__,
                    )
                    return False

    async def stop_to_unconfigured(self) -> None:
        """软停引擎回到「未配模型」态(删当前生效模型用),保留 tick 自愈循环。

        与 stop_engine 的区别:stop_engine 停整个 realtime 循环(含采集/设备同步);
        本方法只关引擎实例 + 降级状态,采集与 tick 继续,后续配好新模型自动自愈拉起。
        """
        async with self._lifecycle_lock:
            await self._pipeline.stop_to_unconfigured()

    async def apply_config_restart(self) -> bool:
        """window_size 变更后重启 runner 使新值生效：停 runner → 启 runner。

        window_size 靠 runner.start() 重读（见 runner.start），只需 stop→start，
        不重建引擎、不重载模型。was_running 时才需重启（未跑时下次 start 自然读新值）。
        全程持 lifecycle 锁,避免与并发的 start_engine/stop_engine 交错。

        （omni_fps 变更不再走这里——改走 ``apply_omni_fps_live`` 运行时热更，见其注释。）

        返回重启是否成功。config 已由调用方写盘(不可回滚),重启失败时返 False 让调用方
        区分「已保存但重启失败」,不冒泡成 500——否则前端会把「写盘成功+重启失败」误报
        成「保存失败」。
        """
        async with self._lifecycle_lock:
            try:
                was_running = self._engine.is_running
                if was_running:
                    await self._engine.stop()
                    await self._engine.start()
                return True
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "[service] 感知参数变更后重启失败(config 已写盘) | %s",
                    e,
                    exc_info=True,
                )
                return False

    async def apply_omni_fps_live(self, omni_fps: int) -> bool:
        """运行时热更 omni_fps（含其顶起的 tracker fps）：不停 runner、不重建引擎、
        不重载模型、不丢在途 track 状态。

        与 ``apply_config_restart`` 并列的轻量入口：只把新 omni_fps 原地推给活跃引擎
        （PipelineProcessor.apply_omni_fps → proxy → engine.apply_omni_fps）。持 lifecycle
        锁避免与 start/stop/restart 交错。引擎未起时 proxy 层 no-op（settings 已写盘）。

        返回是否成功。config 已写盘(不可回滚),失败返 False 不冒泡 500（同 restart 语义）。
        """
        async with self._lifecycle_lock:
            try:
                await self._pipeline.apply_omni_fps(omni_fps)
                return True
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "[service] omni_fps 热更失败(config 已写盘) | %s", e, exc_info=True
                )
                return False

    def engine_status(self) -> PerceptionEngineStatus:
        return self._engine.status()

    @property
    def tier_u_pool(self):
        """暴露 PerceptionEngine 内部的 TierUPool(陌生人池)给 router 用。

        实际穿层封装放在 ``PipelineProcessor.tier_u_pool`` property,本层只透传。
        engine 禁用 / 池启动失败时返 None。
        """
        return self._pipeline.tier_u_pool

    @property
    def deep_sort_config(self):
        """暴露 yaml-resolved DeepSortConfigDC 给 router 视频注册路径用。

        穿层封装放在 ``PipelineProcessor.deep_sort_config``,本层透传。
        engine 未初始化时返代码默认值(``DeepSortConfigDC()``)。
        """
        return self._pipeline.deep_sort_config

    def get_active_confirmed_track_keys(self) -> list[tuple[str, int]]:
        """暴露当前所有 cam 上 confirmed track 的 ``(cam_id, track_id)`` 列表。

        给 router pool_fetch 用: 跟 confirmed track 实时 emb 做去重 (case b)。
        engine 未初始化时返空列表。
        """
        return self._pipeline.get_active_confirmed_track_keys()

    def get_reid_extractor(self):
        """从任一活动的 DeepSortTracker 借 HumanReID 实例,给身份库注册时
        ``add_tier_a_samples_batch`` 做 .npy 兜底抽取用。
        所有 device 的 tracker 共用同一份 ReID ONNX 模型,任选一个即可;
        无活动 tracker → None,库就跳过兜底(行为退回旧版,不报错)。
        """
        return self._pipeline.get_reid_extractor()

    # ---- Buffer management ----

    def clear_buffers(self) -> None:
        """Clear all device stream buffers.

        Discards all buffered data without disconnecting devices.
        New frames arriving after this call start from a clean state,
        allowing the realtime pipeline to process only fresh data.
        """
        self._collector.clear_all_buffers()
        logger.info("All perception buffers cleared")

    # ---- Active perception ----

    async def on_demand_perceive(
        self, request: OnDemandPerceptionRequest
    ) -> OnDemandPerceptionResultItem | None:
        """On-demand perception: batch-collects requested devices and runs
        a single fusion inference via pipeline.

        If the realtime engine is running, data comes from its existing stream
        subscriptions. If not running, the collector may have no data.
        """
        import uuid

        from miloco.perception.schema import OnDemandLogEntry
        from miloco.perception.snapshot_writer import get_snapshot_root

        active_sources = self._collector.get_all_active_sources()

        valid_dids: list[str] = []
        for did in request.sources:
            if did not in active_sources:
                logger.warning("[service](device=%s) 未激活感知(skipped)", did)
                continue
            valid_dids.append(did)

        if not valid_dids:
            raise BusinessException(
                "No valid active perception sources found. "
                "Ensure the perception engine is running and devices are online.",
                code=2011,
            )

        t_start = now_ms()

        # Single batch inference call — collector assembles batch, processor infers
        pipeline_result = await self._pipeline.process_on_demand(
            valid_dids, request.query
        )

        if not pipeline_result:
            raise BusinessException(
                "Failed to perform on-demand perception.",
                code=2012,
            )

        result, artifacts = pipeline_result
        t_end = now_ms()
        log_id = str(uuid.uuid4())

        # Save artifacts (clips + trace) to disk
        clip_dids: list[str] = []
        clip_kinds: dict[str, str] = {}
        has_trace = False

        if not result.answer:
            omni_responded = any(
                call.get("error") is None
                for call in (artifacts.trace or {}).get("calls", [])
            )
            if not omni_responded:
                artifacts.clips = {}

        if artifacts.clips or artifacts.trace:
            from miloco.config.settings import get_settings
            from miloco.perception.snapshot_writer import (
                check_disk_space,
                save_event_artifacts,
            )

            settings = get_settings()
            snapshot_root = get_snapshot_root()
            if check_disk_space(
                snapshot_root, settings.perception.snapshot_min_free_disk_mb
            ):
                clip_dids = save_event_artifacts(log_id, artifacts)
                clip_kinds = {
                    did: artifacts.clips[did][1]
                    for did in clip_dids
                    if did in artifacts.clips
                }
                has_trace = (snapshot_root / log_id / "omni_trace.json.gz").exists()

        # Persist on-demand query log (with artifact metadata).
        # 行写失败必须回滚已落盘的产物:读端点都先过库(router.py:155/194),
        # 行不在 → clip 永远不可达,而 mtime 最新、LRU 最后淘汰,白占共享配额。
        inserted = self._od_log_repo.append(
            OnDemandLogEntry(
                id=log_id,
                timestamp=t_start,
                query=request.query,
                answer=result.answer,
                sources=valid_dids,
                latency_ms=t_end - t_start,
                snapshot_count=len(clip_dids),
                clip_dids=clip_dids,
                clip_kinds=clip_kinds,
                has_trace=has_trace,
            )
        )
        if not inserted:
            logger.error(
                "on_demand_log insert failed for %s; discarding orphaned artifacts",
                log_id,
            )
            if clip_dids or has_trace:
                shutil.rmtree(get_snapshot_root() / log_id, ignore_errors=True)

        # Map inference results back to API response items
        return OnDemandPerceptionResultItem(
            answer=result.answer,
            timestamp=ms_to_iso_local(t_end),
        )

    # ---- Perception logs ----

    def query_logs(
        self,
        after: str | None = None,
        before: str | None = None,
        since: str | None = None,
        limit: int | None = None,
    ) -> dict:
        """Query perception logs.

        Args:
            after: ISO 8601 timestamp cursor — return entries after this time.
            before: ISO 8601 upper bound — return entries before this time.
            since: Relative time string like "1h", "30m", "2h30m".
            limit: Max entries to return. None means no limit.

        Returns:
            Dict with logs, count, and total_inferences.
        """
        from miloco.utils.time_utils import parse_iso_ms, since_to_ms

        after_ms: int | None = None
        before_ms: int | None = None
        since_ms: int | None = None

        if after:
            after_ms = parse_iso_ms(after, "after")

        if before:
            before_ms = parse_iso_ms(before, "before")

        if since and after_ms is None:
            since_ms = since_to_ms(since)

        logs, count = self._log_repo.query(
            after_ms=after_ms, before_ms=before_ms, since_ms=since_ms, limit=limit
        )

        return {
            "logs": logs,
            "count": count,
            "total_inferences": self._log_repo.get_today_inference_count(),
        }

    def cleanup_logs(self, keep_days: int) -> int:
        """清理过期感知日志。"""
        return self._log_repo.delete_before_days(keep_days)

    def runtime_summary(
        self,
        *,
        obs_db_path: Path | str | None = None,
        now_ms_value: int | None = None,
    ) -> PerceptionRuntimeSummary:
        """Return one safe, authenticated summary of the realtime perception state."""
        current_ms = now_ms_value if now_ms_value is not None else now_ms()
        status = self.engine_status()
        log_stats = self._log_repo.runtime_stats()
        meaningful_events_dao = self._get_meaningful_events_dao()

        meaningful_total = meaningful_events_dao.count_all()
        meaningful_last_hour = meaningful_events_dao.count_since(current_ms - 3_600_000)
        raw_total = self._log_repo.count_all()
        raw_last_hour = self._log_repo.count_since(current_ms - 3_600_000)
        last_insert_ms = (
            log_stats.last_insert_ms
            if log_stats.last_insert_ms is not None
            else self._log_repo.latest_timestamp_ms()
        )
        active_sources = list(status.active_sources or [])
        windows = self._query_observability_windows(
            obs_db_path=obs_db_path,
            now_ms=current_ms,
        )
        recent_window = next(
            (window for window in windows if window.get("minutes") == 15),
            windows[0] if windows else {},
        )
        semantic_state, hints = classify_perception_runtime_state(
            running=status.running,
            ready=status.engine.ready,
            active_source_count=len(active_sources),
            raw_last_hour=raw_last_hour,
            meaningful_last_hour=meaningful_last_hour,
            consecutive_empty_descriptions=log_stats.consecutive_empty_descriptions,
            recent_window=recent_window,
        )

        return PerceptionRuntimeSummary(
            now_ms=current_ms,
            engine=RuntimeEngineSummary(
                running=status.running,
                ready=status.engine.ready,
                status=status.engine.status,
                message=status.engine.message,
            ),
            sources=RuntimeSourceSummary(
                active_count=len(active_sources),
                active_sources=active_sources,
            ),
            logs=RuntimeLogSummary(
                today_inference_count=log_stats.today_inference_count,
                raw_total=raw_total,
                raw_last_hour=raw_last_hour,
                last_inference_ms=log_stats.last_inference_ms,
                last_insert_ms=last_insert_ms,
                last_descriptions_empty=log_stats.last_descriptions_empty,
                last_append_inserted=log_stats.last_append_inserted,
                consecutive_empty_descriptions=log_stats.consecutive_empty_descriptions,
                consecutive_deduplicated=log_stats.consecutive_deduplicated,
                meaningful_total=meaningful_total,
                meaningful_last_hour=meaningful_last_hour,
                last_meaningful_event_ms=meaningful_events_dao.latest_timestamp_ms(),
            ),
            windows=[RuntimeWindowSummary(**window) for window in windows],
            latest_omni=self._latest_runtime_omni_summary(),
            semantic_state=semantic_state,
            hints=hints,
        )

    def _query_observability_windows(
        self,
        *,
        obs_db_path: Path | str | None,
        now_ms: int,
        windows_minutes: tuple[int, ...] = (5, 15, 60),
    ) -> list[dict[str, int]]:
        """Aggregate safe perception trace counters for recent time windows."""
        windows = [_zero_observability_window(minutes) for minutes in windows_minutes]
        if obs_db_path is None:
            return windows

        try:
            from miloco.observability.metrics_db import connect

            with connect(obs_db_path) as conn:
                for window in windows:
                    since_ms = now_ms - int(window["minutes"]) * 60_000
                    row = conn.execute(
                        """
                        SELECT
                          COUNT(*) AS cycle_count,
                          SUM(CASE WHEN skipped = 1 THEN 1 ELSE 0 END) AS skipped_count,
                          SUM(CASE WHEN gate_video_pass = 1 THEN 1 ELSE 0 END) AS video_pass_count,
                          SUM(CASE WHEN gate_audio_pass = 1 THEN 1 ELSE 0 END) AS audio_pass_count,
                          SUM(CASE WHEN gate_hold_pass = 1 THEN 1 ELSE 0 END) AS hold_pass_count,
                          COALESCE(SUM(omni_call_count), 0) AS omni_call_count,
                          COALESCE(SUM(omni_error_count), 0) AS omni_error_count,
                          SUM(CASE WHEN cycle_error_msg IS NOT NULL AND cycle_error_msg != '' THEN 1 ELSE 0 END) AS cycle_error_count,
                          COALESCE(MAX(dropped_windows_total), 0) AS dropped_windows_count,
                          COALESCE(MAX(overflow_count_total), 0) AS overflow_count
                        FROM traces
                        WHERE timestamp >= ?
                        """,
                        (since_ms,),
                    ).fetchone()
                    if row is None:
                        continue
                    keys = [
                        "cycle_count",
                        "skipped_count",
                        "video_pass_count",
                        "audio_pass_count",
                        "hold_pass_count",
                        "omni_call_count",
                        "omni_error_count",
                        "cycle_error_count",
                        "dropped_windows_count",
                        "overflow_count",
                    ]
                    for idx, key in enumerate(keys):
                        window[key] = int(row[idx] or 0)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[service] failed to query observability runtime windows: %s",
                type(e).__name__,
            )
        return windows

    def _get_meaningful_events_dao(self):
        dao = getattr(self, "_meaningful_events_dao", None)
        if dao is not None:
            return dao
        from miloco.manager import get_manager

        dao = get_manager().meaningful_events_dao
        self._meaningful_events_dao = dao
        return dao

    def _latest_runtime_omni_summary(self) -> RuntimeOmniSummary | None:
        try:
            from miloco.perception.runtime_diagnostics import (
                classify_omni_response_shape,
                get_runtime_diagnostics,
            )

            sample = get_runtime_diagnostics().latest("realtime")
            if sample is None:
                return None
            classification = classify_omni_response_shape(
                error_code=sample.error_code,
                response_text_length=sample.response_text_length,
                response_json_like=sample.response_json_like,
                parse_ok=sample.parse_ok,
                skipped=sample.skipped,
                caption_count=sample.caption_count,
                matched_rule_count=sample.matched_rule_count,
                suggestion_count=sample.suggestion_count,
                speech_count=sample.speech_count,
            )
            return RuntimeOmniSummary(
                timestamp_ms=sample.timestamp_ms,
                protocol=sample.protocol,
                route=sample.route,
                request={
                    "message_count": sample.message_count,
                    "text_block_count": sample.text_block_count,
                    "image_block_count": sample.image_block_count,
                    "video_block_count": sample.video_block_count,
                    "audio_block_count": sample.audio_block_count,
                },
                response={
                    "text_length": sample.response_text_length,
                    "json_like": sample.response_json_like,
                    "parse_ok": sample.parse_ok,
                    "skipped": sample.skipped,
                    "caption_count": sample.caption_count,
                    "matched_rule_count": sample.matched_rule_count,
                    "suggestion_count": sample.suggestion_count,
                    "speech_count": sample.speech_count,
                    "complete_speech_count": sample.complete_speech_count,
                    "needs_response_speech_count": sample.needs_response_speech_count,
                    "classification": classification,
                },
                error_code=sample.error_code,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[service] failed to read runtime omni diagnostics: %s",
                type(e).__name__,
            )
            return None

    # ---- On-demand logs ----

    def query_on_demand_logs(
        self,
        since_ms: int | None = None,
        before_ms: int | None = None,
        before_id: str | None = None,
        limit: int | None = None,
    ) -> dict:
        """Query on-demand perception query logs.

        Args:
            since_ms: Unix ms lower bound (inclusive).
            before_ms: Unix ms upper bound (exclusive).
            before_id: Compound cursor tiebreaker (used with before_ms).
            limit: Max entries to return.
        """
        logs = self._od_log_repo.query(
            since_ms=since_ms, before_ms=before_ms, before_id=before_id, limit=limit
        )
        if not logs:
            return {"logs": logs}

        from miloco.perception.events_service import EventsService
        from miloco.perception.snapshot_writer import get_snapshot_root

        snapshot_root = get_snapshot_root()
        fb_index = EventsService.build_feedback_index()
        for row in logs:
            # has_trace 从磁盘派生（trace 可被 TTL/LRU 清掉）；clip_dids 留 DB 值，
            # 缺失时 clip 端点返 410 降级——两者有意不对称。
            row["has_trace"] = (
                snapshot_root / row["id"] / "omni_trace.json.gz"
            ).exists()
            fb = fb_index.get(row["id"])
            row["has_feedback"] = fb is not None
            row["feedback_pack_path"] = fb[0] if fb else None
            row["feedback_pack_size"] = fb[1] if fb else None

        return {"logs": logs}

    def get_on_demand_log(self, log_id: str) -> dict | None:
        """Get a single on-demand log entry by ID."""
        row = self._od_log_repo.get_by_id(log_id)
        if row is not None:
            from miloco.perception.snapshot_writer import get_snapshot_root

            row["has_trace"] = (
                get_snapshot_root() / row["id"] / "omni_trace.json.gz"
            ).exists()
        return row

    def cleanup_on_demand_logs(self, keep_days: int) -> int:
        """清理过期主动查询日志。"""
        return self._od_log_repo.delete_before_days(keep_days)

    # ---- Device management ----

    async def get_devices(self, online_only: bool = True) -> list[PerceptionDevice]:
        """List all perception-capable devices across all adapter types.

        Args:
            online_only: If True (default), only return online devices.
                         If False, return all discovered devices.
        """
        devices: list[PerceptionDevice] = []

        for adapter in self._collector._adapters.values():
            try:
                # cap=False：列设备全集（含超出投喂上限的相机），用于 rule target
                # 校验等「枚举可选设备」语义，不受 MAX_ENABLED_CAMERAS 投喂上限收窄。
                discovered = await adapter.discover_devices(
                    online_only=online_only, cap=False
                )
                for did, source in discovered.items():
                    devices.append(source)
            except Exception as e:
                logger.error(
                    "[collect](adapter=%s) 发现设备失败 | %s",
                    adapter.device_type,
                    e,
                )

        return devices
