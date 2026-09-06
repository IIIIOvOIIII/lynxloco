"""
Realtime Perception Engine.

Scheduler that delegates perception to the pipeline processor.
Device sync runs on its own timer, decoupled from perception ticks.

The perception loop reacts to two triggers:
1. **Window-ready event** — fired by MultiTrackSyncBuffer when a time
   window has data from all tracks (early trigger).
2. **Capture interval timeout** — fallback timer that fires even when
   not all tracks have arrived within the window.
"""

import asyncio
import logging
import time
from collections import Counter

from miloco.config import get_settings
from miloco.database.perception_repo import PerceptionLogRepo
from miloco.perception import omni_probe_registry
from miloco.perception import window_runtime as windows
from miloco.perception.collect.collector import MultimodalCollector
from miloco.perception.inference_worker import InferenceWorker
from miloco.perception.processor import PipelineProcessor
from miloco.perception.schema import EngineState, PerceptionEngineStatus

logger = logging.getLogger(__name__)


class PerceptionRunner:
    """Background engine that schedules periodic perception cycles."""

    def __init__(
        self,
        collector: MultimodalCollector,
        pipeline: PipelineProcessor,
        log_repo: PerceptionLogRepo,
        window_ready_event: asyncio.Event | None = None,
    ):
        self._collector = collector
        self._pipeline = pipeline
        self._log_repo = log_repo

        self._collect_interval = get_settings().perception.collect.window_size
        self._is_running = False
        self._perception_task: asyncio.Task | None = None
        self._sync_devices_task: asyncio.Task | None = None
        self._window_ready = window_ready_event
        self._window_tasks: dict[asyncio.Task, windows.WindowJob] = {}
        self._last_window: dict[str, windows.WindowJob] = {}
        self._window_sequence: Counter = Counter()
        self._window_counts: Counter = Counter()
        self._camera_cursor = 0
        self._run_generation = 0

        # Persistent worker thread with a durable event loop for inference.
        # Replaces the old ThreadPoolExecutor + asyncio.run() pattern that
        # leaked threads via repeated default executor creation. Safe to
        # stop() then start() again on this same instance — see its
        # docstring for how it isolates each restart's thread/loop so a
        # still-draining previous generation never blocks or races the new
        # one.
        self._inference_worker = InferenceWorker(thread_name="perception-infer")

    @property
    def is_running(self) -> bool:
        return self._is_running

    def status(self) -> PerceptionEngineStatus:
        sources = self._collector.get_all_active_sources()
        last_latency = self._pipeline.last_latency
        return PerceptionEngineStatus(
            running=self._is_running,
            engine=EngineState(
                ready=self._pipeline.engine_ready,
                status=self._pipeline.engine_status,
                message=self._pipeline.engine_status_message,
            ),
            interval_seconds=self._collect_interval,
            today_inference_count=self._log_repo.get_today_inference_count(),
            active_sources=[
                {
                    "did": s.did,
                    "name": s.name,
                    "device_type": s.device_type,
                    "room_name": s.room_name,
                }
                for s in sources.values()
            ],
            last_latency=last_latency.to_dict() if last_latency else None,
        )

    async def start(self) -> None:
        """Start the realtime perception loop and device sync loop."""
        if self._is_running:
            logger.warning("[engine] 引擎已在运行，忽略重复启动")
            return

        self._is_running = True
        self._run_generation += 1
        self._last_window.clear()

        # 重启时重读窗口时长（config 可能在停止期间被改）——__init__ 只读一次，
        # 不重读会导致「应用设置」改了 window_size 后引擎仍按旧值跑。
        self._collect_interval = get_settings().perception.collect.window_size

        # Restart worker if it was stopped by a previous stop(). Safe even
        # if that previous generation's thread hasn't exited yet (it may
        # still be draining a non-preemptible ONNX call) — start() spins up
        # a new generation on this same instance without waiting on the old
        # one; see InferenceWorker's docstring.
        if not self._inference_worker.is_running:
            self._inference_worker.start()

        # 显式启动/重启(含「重启感知」按钮):全可恢复态重建一次,含 engine_init_failed
        # ——引擎构造失败(如临时磁盘满)补救后靠这条恢复,不在 tick 每秒重试重型构造。
        # 必须在 set_inference_worker 之前,确保引擎已存在再挂 worker。
        self._pipeline.try_reinit_engine(include_failed=True)

        # Attach inference worker to engine proxy so perceive calls
        # run in the dedicated thread, not on the main event loop.
        self._pipeline.set_inference_worker(self._inference_worker)

        # Initial device sync before first tick
        await self._collector.sync_all_devices()

        self._perception_task = asyncio.create_task(self._perception_loop())
        self._sync_devices_task = asyncio.create_task(self._sync_devices_loop())

        logger.info("Perception engine started")

    async def stop(self) -> None:
        """Stop the realtime perception loop and shutdown collector."""
        if not self._is_running:
            logger.warning("[engine] 引擎未运行，忽略重复停止")
            return

        self._is_running = False

        for task in (
            self._perception_task,
            self._sync_devices_task,
        ):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._perception_task = None
        self._sync_devices_task = None

        await self._cancel_windows()

        # 清理 in-flight probe task,防同进程再启 runner 时 _probe_in_flight 残留导致
        # 自愈通道永久卡死。registry 是独立 module,不进 runner↔processor 循环链。
        await omni_probe_registry.cancel_inflight()

        # 关闭 perception engine（含 IdentityEngine dispatcher worker 等）
        try:
            await self._pipeline.close()
        except Exception as e:  # noqa: BLE001
            logger.error("[engine] 关闭引擎失败 | %s", e)

        self._inference_worker.shutdown(wait=False)
        await self._collector.shutdown()
        logger.info("Perception engine stopped")

    async def _tick(self) -> None:
        """Drain all ready windows and infer sequentially."""
        # 每 tick 驱动一次 omni 熔断器自动探测:OPEN_RECOVERABLE + backoff 到期时 spawn
        # 一次后台 probe。无外部驱动时 probe_due 归零后状态永远不动,provider 恢复后感知
        # 也不会自愈,只能靠用户手动点「立即重试」。sync 判断 + 后台 spawn,tick 不阻塞。
        # 前置到 active_sources 判断之前:无摄像头联调 / 摄像头全部掉线场景下也要能自愈,
        # 否则 backoff 到期后 next_probe_at_monotonic 卡在过去、SSE 快照持续显示"0 秒后下次探测"。
        # 非 OPEN_RECOVERABLE 时 try_arm_probe 零开销直接返 False,前置安全。
        self._pipeline.drive_omni_probe()

        if not self._collector.get_all_active_sources():
            return

        # 每个 tick 自愈一次:出厂态配好 key / 补完模型后,下个推理周期(默认 4s)自动转
        # ready,与 omni_client.resolve_live_omni_config 注释承诺的"下个推理周期热生效"
        # 对齐。只放行廉价"等外部条件"态(缺 key/模型),engine_init_failed 不在此重试
        # (见 try_reinit);配合 STARTING 后移,未满足前置条件时零开销、零 event_log 噪声。
        self._pipeline.try_reinit_engine()

        if windows.concurrency() > 1 or self._window_tasks:
            await self._tick_concurrent()
            return

        result = await self._pipeline.process_realtime()
        # 缓冲区里可能积压了多个 ready 窗口，此处循环处理直到缓冲区清空
        while result is not None and self._is_running and windows.concurrency() == 1:
            result = await self._pipeline.process_realtime()

    async def _cancel_windows(self) -> None:
        tasks = list(self._window_tasks)
        for task in tasks:
            self._window_tasks[task].valid = False
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._window_tasks.clear()
        self._last_window.clear()

    def concurrency_status(self) -> dict:
        return {**windows.HTTP_LIMITER.snapshot(),
                "jobs": len(self._window_tasks),
                "generation": self._run_generation,
                "windows": dict(self._window_counts)}

    async def _process_window(self, job, batch):
        with windows.window_scope(job):
            try:
                remaining = windows.MAX_WINDOW_AGE_SECONDS - max(
                    0, time.time() - job.end_unix_ms / 1000)
                if remaining <= 0:
                    self._window_counts["expired_before_prepare"] += 1
                    self._pipeline.record_window_gap(batch, "expired_before_prepare")
                    return
                async with asyncio.timeout(remaining):
                    await self._pipeline.process_realtime(batch=batch)
                self._window_counts["completed"] += 1
            except TimeoutError:
                job.valid = False
                self._window_counts["deadline_gap"] += 1
                logger.warning("[windows] deadline gap camera=%s sequence=%d", job.camera, job.sequence)
            except asyncio.CancelledError:
                job.valid = False
                self._window_counts["cancelled_gap"] += 1
                raise
            except Exception:
                self._window_counts["failed_gap"] += 1
                logger.exception("[windows] failed gap camera=%s sequence=%d", job.camera, job.sequence)
            finally:
                job.finish()

    async def _tick_concurrent(self) -> None:
        sources = list(self._collector.get_all_active_sources())
        if not sources:
            return
        # Round-robin over cameras, with a strict count bound independent of
        # input rate. Only one unencoded window is prepared at any instant.
        admitted = 0
        empty = set()
        cap = windows.concurrency()
        per_camera = (cap + len(sources) - 1) // len(sources) + windows.MAX_WAITING_PER_CAMERA
        while self._is_running and len(self._window_tasks) < cap + windows.MAX_WAITING:
            did = sources[self._camera_cursor % len(sources)]
            self._camera_cursor += 1
            if did in empty:
                if len(empty) == len(sources):
                    break
                continue
            if sum(j.camera == did for j in self._window_tasks.values()) >= per_camera:
                empty.add(did)
                continue
            batch = self._collector.collect_batch([did], drain=True, fifo=True)
            if batch.empty:
                empty.add(did)
                continue
            dd = batch.devices[did]
            generation = (self._run_generation << 32) + dd.source_generation
            previous = self._last_window.get(did)
            if previous is not None and previous.generation != generation:
                for task, old in list(self._window_tasks.items()):
                    if old.camera == did:
                        old.valid = False
                        task.cancel()
                previous = None
            self._window_sequence[did] += 1
            job = windows.WindowJob(did, generation, self._window_sequence[did],
                                    batch.end_timestamp, previous=previous,
                                    config_version=str(cap))
            self._last_window[did] = job
            task = asyncio.create_task(self._process_window(job, batch))
            self._window_tasks[task] = job
            def done(task):
                self._window_tasks.pop(task, None)
                if self._window_ready is not None:
                    self._window_ready.set()
            task.add_done_callback(done)
            self._window_counts["admitted"] += 1
            # The worker releases raw arrays immediately after encoding, before
            # it waits for an HTTP slot. Do not accumulate raw BGR in this loop.
            await asyncio.shield(asyncio.wrap_future(job.prepared))
            admitted += 1
            if admitted >= cap + windows.MAX_WAITING:
                break

    async def _wait_for_trigger(self) -> None:
        """Wait for window-ready event OR capture interval timeout.

        If a window_ready_event is provided, we race it against the timer.
        The event is cleared after waking so the next cycle can wait again.
        """
        if self._window_ready is not None:
            try:
                await asyncio.wait_for(
                    self._window_ready.wait(),
                    timeout=self._collect_interval,
                )
            except TimeoutError:
                pass
            finally:
                self._window_ready.clear()
        else:
            await asyncio.sleep(self._collect_interval)

    async def _perception_loop(self) -> None:
        """Perception loop — wakes on window-ready or timeout.

        Each cycle: run one tick (drains all ready windows), then wait for
        the next trigger.
        """
        while self._is_running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[runner] 单次感知循环失败 | %s", e, exc_info=True)

            try:
                await self._wait_for_trigger()
            except asyncio.CancelledError:
                break

    async def _sync_devices_loop(self) -> None:
        """Device sync loop — runs independently from perception ticks."""
        while self._is_running:
            try:
                active_devices = self._collector.get_all_active_sources()
                await asyncio.sleep(10 if len(active_devices) > 0 else 1)
            except asyncio.CancelledError:
                break

            try:
                await self._collector.sync_all_devices()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[runner] 设备同步失败 | %s", e, exc_info=True)
