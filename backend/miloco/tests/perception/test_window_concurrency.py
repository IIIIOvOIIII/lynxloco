"""Runtime contract: bounded HTTP, ordered per-camera effects and lifecycle."""

import asyncio
import time

import pytest
from miloco.perception import window_runtime as runtime


def test_window_remains_actionable_after_120_seconds_until_180(monkeypatch):
    monkeypatch.setattr(runtime.time, "time", lambda: 1000.0)
    assert runtime.WindowJob("cam", 0, 0, 850_000).actionable
    assert runtime.WindowJob("cam", 0, 1, 820_001).actionable
    assert not runtime.WindowJob("cam", 0, 2, 819_999).actionable


@pytest.mark.parametrize("cap", [4, 8])
async def test_same_camera_http_requests_overlap_and_commit_in_order(cap):
    limiter = runtime.RequestLimiter(lambda: cap)
    admitted = asyncio.Event()
    release = asyncio.Event()
    active = peak = 0
    commits = []
    previous = None
    jobs = []
    for i in range(cap):
        job = runtime.WindowJob("cam", 0, i, time.time() * 1000, previous=previous)
        jobs.append(job)
        previous = job

    async def run(job):
        nonlocal active, peak
        with runtime.window_scope(job):
            async with limiter.slot("cam", 10):
                active += 1
                peak = max(peak, active)
                if active == cap:
                    admitted.set()
                await release.wait()
                active -= 1
            await asyncio.sleep((cap - job.sequence) / 1000)
            await job.begin_commit()
            commits.append(job.sequence)
            job.finish()

    tasks = [asyncio.create_task(run(j)) for j in jobs]
    await asyncio.wait_for(admitted.wait(), 1)
    assert peak == cap
    release.set()
    await asyncio.gather(*tasks)
    assert commits == list(range(cap))
    assert limiter.active == limiter.payload_bytes == 0


async def test_live_resize_8_to_4_does_not_admit_until_below_new_cap():
    cap = 8
    limiter = runtime.RequestLimiter(lambda: cap)
    releases = [asyncio.Event() for _ in range(9)]
    started = []

    async def run(i):
        async with limiter.slot("cam", 1):
            started.append(i)
            await releases[i].wait()

    tasks = [asyncio.create_task(run(i)) for i in range(9)]
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(started) == 8
    cap = 4
    for i in range(4):
        releases[i].set()
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(started) == 8
    releases[4].set()
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(started) == 9
    for event in releases:
        event.set()
    await asyncio.gather(*tasks)


async def test_payload_budget_and_cancelled_waiter_release_capacity():
    limiter = runtime.RequestLimiter(lambda: 1, max_payload_bytes=20)
    async with limiter.slot("cam", 10):
        with pytest.raises(runtime.WindowRejected, match="bytes"):
            async with limiter.slot("other", 11):
                pass
        task = asyncio.create_task(_wait_slot(limiter))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert limiter.payload_bytes == 10
    assert limiter.payload_bytes == 0


async def _wait_slot(limiter):
    async with limiter.slot("other", 5):
        pass


def test_action_age_retains_normal_grok_latency_but_rejects_history():
    now = time.time() * 1000
    assert runtime.WindowJob("cam", 0, 1, now - 40_000).actionable
    assert not runtime.WindowJob("cam", 0, 2, now - 181_000).actionable


@pytest.mark.parametrize("cap", [4, 8])
@pytest.mark.parametrize(
    "entrypoint", ["engine", "runner", "runner_restart", "runner_timeout"]
)
async def test_real_engine_serial_preparation_concurrent_http_ordered_results(
    monkeypatch, cap, entrypoint
):
    import json
    from unittest.mock import AsyncMock

    import httpx
    import numpy as np
    from miloco.perception.engine import pipeline
    from miloco.perception.engine.api import PerceptionEngine
    from miloco.perception.engine.config import PerceptionConfig
    from miloco.perception.engine.types import GatePacket, GateTiming, GateTrigger
    from miloco.perception.types import BatchedSnapshot, PerceptionDevice
    from miloco.perception.utils import snapshot_from_arrays

    config = PerceptionConfig()
    config.identity_engine.enabled = False
    config.omni.api_key = "synthetic-test-only"
    config.omni.model = "test"
    config.omni.base_url = "https://example.invalid"
    engine = PerceptionEngine(config)
    import concurrent.futures

    release = concurrent.futures.Future()
    all_started = concurrent.futures.Future()
    http_active = peak = gate_active = gate_peak = 0
    prepared = []
    completed = []

    async def gate(snapshot, *args, **kwargs):
        nonlocal gate_active, gate_peak
        gate_active += 1
        gate_peak = max(gate_peak, gate_active)
        await asyncio.sleep(0)
        prepared.append(runtime.current_job().sequence)
        gate_active -= 1
        return (
            GatePacket(
                "p",
                "room",
                snapshot.start_timestamp,
                GateTrigger(True, 1, False, 0),
                snapshot.frames,
                np.zeros(0, np.int16),
            ),
            GateTiming(1, 0, True, False),
            None,
            None,
            None,
        )

    class FakeHTTP:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            nonlocal http_active, peak
            i = runtime.current_job().sequence
            http_active += 1
            peak = max(peak, http_active)
            if http_active == cap and not all_started.done():
                all_started.set_result(None)
            try:
                await asyncio.shield(asyncio.wrap_future(release))
                await asyncio.sleep((cap - i) / 1000)
            finally:
                http_active -= 1
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "caption": str(i),
                                        "matched_rules": [],
                                        "speeches": [],
                                        "suggestions": [],
                                    }
                                )
                            }
                        }
                    ]
                },
            )

    monkeypatch.setattr(pipeline, "run_gate", gate)
    monkeypatch.setattr(pipeline, "resolve_live_omni_config", lambda c: c)
    monkeypatch.setattr(runtime, "model_identity", lambda config=None: "synthetic")
    monkeypatch.setattr(httpx, "AsyncClient", FakeHTTP)
    monkeypatch.setattr(runtime, "HTTP_LIMITER", runtime.RequestLimiter(lambda: cap))
    monkeypatch.setattr(
        "miloco.perception.engine.omni.omni_client.fire_record", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "miloco.perception.engine.omni.omni_client.get_omni_circuit_breaker",
        lambda: AsyncMock(),
    )
    # Only media compression is substituted; API, Gate/identity sequencing,
    # prompt route, HTTP client and response merge are the production path.
    monkeypatch.setattr(
        "miloco.perception.engine.omni.omni.build_batch_prompt",
        lambda *a, **k: {"system_prompt": "test", "user_content": "test"},
    )
    previous = None
    jobs = []
    for i in range(cap):
        job = runtime.WindowJob("cam", 0, i, time.time() * 1000, previous=previous)
        jobs.append(job)
        previous = job

    async def run(job):
        snapshot = snapshot_from_arrays(
            PerceptionDevice(
                did="cam", name="cam", device_type="camera", room_name="room"
            ),
            frames=[np.zeros((32, 32, 3), np.uint8)],
            audio=np.zeros(0, np.int16),
            start_timestamp=job.end_unix_ms - 4000,
            end_timestamp=job.end_unix_ms,
        )
        with runtime.window_scope(job):
            try:
                result = await engine.realtime_perceive(
                    BatchedSnapshot(snapshots=[snapshot])
                )
                completed.append(result.caption[0].description)
            finally:
                job.finish()

    runner = None
    if entrypoint == "engine":
        tasks = [asyncio.create_task(run(j)) for j in jobs]
    else:
        from collections import deque
        from types import SimpleNamespace
        from unittest.mock import Mock

        from miloco.perception.client import PerceptionEngineProxy
        from miloco.perception.processor import PipelineProcessor
        from miloco.perception.runner import PerceptionRunner
        from miloco.perception.schema import (
            DecodedVideoFrame,
            DeviceData,
            PerceptionBatch,
        )

        pending = deque()
        device = PerceptionDevice(
            did="cam", name="cam", device_type="camera", room_name="room"
        )
        for job in jobs:
            dd = DeviceData(
                meta=device,
                video=[
                    DecodedVideoFrame(
                        frame=np.zeros((32, 32, 3), np.uint8), stream_ts=0
                    )
                ],
                window_start_unix_ms=int(job.end_unix_ms - 4000),
                window_end_unix_ms=int(job.end_unix_ms),
            )
            if entrypoint == "runner_timeout" and job.sequence == 0:
                dd.dropped_windows = 4
            pending.append(PerceptionBatch(devices={"cam": dd}))
        collector = Mock()
        collector.get_all_active_sources.return_value = {"cam": device}
        collector.collect_batch.side_effect = lambda *a, **k: (
            pending.popleft() if pending else PerceptionBatch()
        )
        proxy = PerceptionEngineProxy.__new__(PerceptionEngineProxy)
        proxy.perception_engine = engine
        proxy._engine_lock = asyncio.Lock()
        proxy._concurrent_calls = {}
        proxy._closing = False
        proxy._status = "ready"
        proxy._status_message = ""
        proxy._inference_worker = None

        async def final(result, **kwargs):
            completed.append(result.caption[0].description)

        proxy.handle_realtime_perception_result = final
        monkeypatch.setattr(
            "miloco.manager.get_manager",
            lambda: SimpleNamespace(
                rule_service=SimpleNamespace(get_all_rules=AsyncMock(return_value=[]))
            ),
        )
        processor = PipelineProcessor(collector, proxy, Mock())
        processor._perf_enabled = entrypoint == "runner_timeout"
        metrics = Mock()
        monkeypatch.setattr(
            "miloco.perception.processor.get_metrics_client", lambda: metrics
        )
        processor.drive_omni_probe = lambda: None
        processor.try_reinit_engine = lambda: None
        monkeypatch.setattr(runtime, "concurrency", lambda: cap)
        runner = PerceptionRunner(collector, processor, Mock())
        runner._is_running = True
        runner._window_sequence["cam"] = -1
        runner._inference_worker.start()
        processor.set_inference_worker(runner._inference_worker)
        if entrypoint == "runner_timeout":
            monkeypatch.setattr(runtime, "MAX_WINDOW_AGE_SECONDS", 0.1)
        await asyncio.wait_for(runner._tick(), 2)
        tasks = list(runner._window_tasks)
        if entrypoint in {"runner_restart", "runner_timeout"}:
            await asyncio.wait_for(asyncio.wrap_future(all_started), 2)
            if entrypoint == "runner_restart":
                await runner._cancel_windows()
            else:
                await asyncio.wait_for(asyncio.gather(*tasks), 1)
                assert runner._window_counts["deadline_gap"] == cap
                assert metrics.publish_trace.call_count == cap
                cycles = [call.args[0] for call in metrics.publish_trace.call_args_list]
                assert sum(c.omni_request_count for c in cycles) == cap
                assert sum(c.dropped_windows_total for c in cycles) == 4
                assert all(c.omni_wall_ms > 0 for c in cycles)
                assert all(c.cycle_error_msg for c in cycles)
                assert all(c.in_delay_ms < c.cycle_total_ms / 2 for c in cycles)
            await proxy.close()
            assert runtime.HTTP_LIMITER.active == 0
            assert runtime.HTTP_LIMITER.payload_bytes == 0
            assert completed == []
            assert engine._last_captions == {}
            assert engine._window_locks == {}
            runner._inference_worker.shutdown(wait=True)
            release.set_result(None)
            # Same engine/worker object, new loop and source generation.
            release = concurrent.futures.Future()
            all_started = concurrent.futures.Future()
            prepared.clear()
            runner._last_window.clear()
            runner._run_generation += 1
            runner._window_sequence["cam"] = -1
            monkeypatch.setattr(runtime, "MAX_WINDOW_AGE_SECONDS", 120.0)
            for _ in range(cap):
                end = int(time.time() * 1000)
                pending.append(
                    PerceptionBatch(
                        devices={
                            "cam": DeviceData(
                                meta=device,
                                video=[
                                    DecodedVideoFrame(
                                        frame=np.zeros((32, 32, 3), np.uint8),
                                        stream_ts=0,
                                    )
                                ],
                                window_start_unix_ms=end - 4000,
                                window_end_unix_ms=end,
                            )
                        }
                    )
                )
            runner._inference_worker.start()
            processor.set_inference_worker(runner._inference_worker)
            await asyncio.wait_for(runner._tick(), 2)
            tasks = list(runner._window_tasks)
    try:
        await asyncio.wait_for(asyncio.wrap_future(all_started), 2)
        assert gate_peak == 1
        assert peak == cap
    finally:
        release.set_result(None)
        await asyncio.gather(*tasks)
        if runner is not None:
            await proxy.close()
            runner._inference_worker.shutdown(wait=True)
    assert prepared == list(range(cap))
    assert completed == [str(i) for i in range(cap)]
    assert engine._last_captions["cam"] == str(cap - 1)
    assert runtime.HTTP_LIMITER.active == 0
    assert runtime.HTTP_LIMITER.payload_bytes == 0


async def test_runner_fair_bounded_admission_while_one_camera_is_slow(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from miloco.perception.runner import PerceptionRunner
    from miloco.perception.schema import DeviceData, PerceptionBatch
    from miloco.perception.types import PerceptionDevice

    started = []
    release = asyncio.Event()
    source = {
        d: PerceptionDevice(did=d, name=d, device_type="camera") for d in ("a", "b")
    }

    class Collector:
        def get_all_active_sources(self):
            return source

        def collect_batch(self, dids=None, *, drain=True, fifo=False):
            assert fifo
            did = dids[0]
            return PerceptionBatch(
                devices={
                    did: DeviceData(
                        meta=source[did],
                        video=[object()],
                        window_end_unix_ms=int(time.time() * 1000),
                    )
                }
            )

    class Processor:
        def drive_omni_probe(self):
            pass

        def try_reinit_engine(self):
            pass

        async def process_realtime(self, *, batch=None):
            job = runtime.current_job()
            started.append(job.camera)
            job.release_preparation()
            await release.wait()
            return True

    monkeypatch.setattr(
        "miloco.perception.runner.get_settings",
        lambda: SimpleNamespace(
            perception=SimpleNamespace(collect=SimpleNamespace(window_size=4))
        ),
    )
    monkeypatch.setattr(runtime, "concurrency", lambda: 4)
    runner = PerceptionRunner(Collector(), Processor(), Mock())
    runner._is_running = True
    try:
        await asyncio.wait_for(runner._tick(), 1)
        assert started[:4] == ["a", "b", "a", "b"]
        assert len(started) <= 12
        assert len(runner._window_tasks) == len(started)
    finally:
        release.set()
        if hasattr(runner, "_window_tasks"):
            await asyncio.gather(*runner._window_tasks)


async def test_fused_pending_isolated_and_out_of_order_response_commits_own_tracks(
    monkeypatch,
):
    import json
    from types import SimpleNamespace

    from miloco.perception.engine.config import OmniConfig
    from miloco.perception.engine.identity.dispatcher import (
        FusedDispatcher,
        IdentityQueryItem,
    )
    from miloco.perception.engine.omni import omni
    from miloco.perception.engine.types import OmniContext

    dispatcher = FusedDispatcher()
    callbacks = []

    class Identity:
        library = SimpleNamespace(list_persons=lambda: [])
        config = SimpleNamespace(
            stranger=SimpleNamespace(distinguish=True), confidence_cutoff=0.5
        )

        def take_fused_pending(self):
            return dispatcher.take_pending()

        async def deliver_fused_response(self, a):
            await dispatcher.deliver_response(a)

        async def deliver_fused_failure(self, r):
            await dispatcher.deliver_failure(r)

    identity = Identity()
    first = runtime.WindowJob("cam", 0, 1, time.time() * 1000)
    second = runtime.WindowJob("cam", 0, 2, time.time() * 1000, previous=first)
    for job in [first, second]:
        with runtime.window_scope(job):

            async def callback(result, seq=job.sequence):
                callbacks.append((seq, result.track_id, result.omni_answered))

            await dispatcher.dispatch([IdentityQueryItem(job.sequence)], {}, callback)
    monkeypatch.setattr(omni, "build_fused_payload", lambda **k: {"messages": []})

    async def http(*a, **k):
        seq = runtime.current_job().sequence
        await asyncio.sleep((3 - seq) / 1000)
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "caption": str(seq),
                                "identity_assignments": [
                                    {
                                        "track_id": seq,
                                        "name": "unknown",
                                        "confidence": 0.9,
                                    }
                                ],
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(omni, "_call_omni_messages", http)

    async def run(job):
        with runtime.window_scope(job):
            try:
                await omni.run_omni_fused([], OmniContext(), OmniConfig(), identity)
            finally:
                job.finish()

    await asyncio.gather(run(first), run(second))
    assert callbacks == [(1, 1, True), (2, 2, True)]
    assert dispatcher._pending_jobs == {}


async def test_cancelled_fused_window_cleans_only_its_own_pending(monkeypatch):
    from types import SimpleNamespace

    from miloco.perception.engine.config import OmniConfig
    from miloco.perception.engine.identity.dispatcher import (
        FusedDispatcher,
        IdentityQueryItem,
    )
    from miloco.perception.engine.omni import omni
    from miloco.perception.engine.types import OmniContext

    dispatcher = FusedDispatcher()
    results = []

    async def callback(result):
        results.append(result)

    class Identity:
        library = SimpleNamespace(list_persons=lambda: [])

        def take_fused_pending(self):
            return dispatcher.take_pending()

        async def deliver_fused_failure(self, r):
            await dispatcher.deliver_failure(r)

    one = runtime.WindowJob("cam", 0, 1, time.time() * 1000)
    two = runtime.WindowJob("cam", 0, 2, time.time() * 1000)
    for job in [one, two]:
        with runtime.window_scope(job):
            await dispatcher.dispatch([IdentityQueryItem(job.sequence)], {}, callback)
    monkeypatch.setattr(omni, "build_fused_payload", lambda **k: {"messages": []})
    started = asyncio.Event()

    async def http(*a, **k):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(omni, "_call_omni_messages", http)
    with runtime.window_scope(one):
        task = asyncio.create_task(
            omni.run_omni_fused([], OmniContext(), OmniConfig(), Identity())
        )
    await started.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert [(r.track_id, r.omni_answered) for r in results] == [(1, False)]
    assert set(dispatcher._pending_jobs) == {two.job_id}
    await dispatcher.close()


async def test_expired_identity_response_and_reused_track_cannot_mutate_current_state():
    from miloco.perception.engine.identity.dispatcher import OmniIdentityResult
    from miloco.perception.engine.identity.engine import IdentityEngine
    from miloco.perception.engine.identity.state import TrackIdentityState

    engine = IdentityEngine.__new__(IdentityEngine)
    original = TrackIdentityState(track_id=1, status="confirmed", inflight=True)
    engine._states = {1: original}
    job = runtime.WindowJob("cam", 0, 1, time.time() * 1000 - 181_000)
    with runtime.window_scope(job):
        callback = engine._make_on_result(now_ts=time.time())
        await callback(OmniIdentityResult(1, None, 0.0, "late", no_person=True))
        assert original.status == "confirmed"
        assert original.inflight is False
        replacement = TrackIdentityState(track_id=1, status="pending", inflight=True)
        engine._states[1] = replacement
        await callback(OmniIdentityResult(1, "somebody", 1.0, "old generation"))
        assert replacement.status == "pending"
        assert replacement.inflight is True


async def test_http_waiters_rotate_cameras_and_enforce_waiting_count():
    limiter = runtime.RequestLimiter(lambda: 1)
    order = []

    async def run(camera):
        async with limiter.slot(camera, 1):
            order.append(camera)
            await asyncio.sleep(0)

    async with limiter.slot("busy", 1):
        tasks = [asyncio.create_task(run(c)) for c in ["a"] * 4 + ["b"] * 4]
        for _ in range(10):
            await asyncio.sleep(0)
        with pytest.raises(runtime.WindowRejected, match="waiting_full"):
            async with limiter.slot("c", 1):
                pass
    await asyncio.gather(*tasks)
    assert order == ["a", "b"] * 4
    assert limiter.payload_bytes == 0


async def test_reorder_gap_deadline_advances_without_negative_evidence(monkeypatch):
    monkeypatch.setattr(runtime, "MAX_WINDOW_AGE_SECONDS", 0.02)
    previous = runtime.WindowJob("cam", 0, 1, time.time() * 1000 - 19)
    current = runtime.WindowJob("cam", 0, 2, time.time() * 1000)
    current.previous = previous
    assert await asyncio.wait_for(current.begin_commit(), 0.1)
    assert previous.valid is False
    assert previous.completed.done()
    current.finish()


async def test_encoded_budget_remains_reserved_until_ordered_result_is_released():
    limiter = runtime.RequestLimiter(lambda: 8, max_payload_bytes=20)
    job = runtime.WindowJob("cam", 0, 1, time.time() * 1000)
    async with limiter.slot("cam", 15, retain_for=job):
        pass
    assert limiter.active == 0
    assert limiter.payload_bytes == 15
    with pytest.raises(runtime.WindowRejected):
        async with limiter.slot("other", 10):
            pass
    job.finish()
    assert limiter.payload_bytes == 0


def test_model_identity_change_invalidates_old_results_but_resize_does_not(monkeypatch):
    from types import SimpleNamespace

    omni = SimpleNamespace(
        model="first",
        api_protocol="openai_chat_completions",
        base_url="https://one.invalid",
        concurrency=8,
    )
    monkeypatch.setattr(
        "miloco.config.get_settings",
        lambda: SimpleNamespace(model=SimpleNamespace(omni=omni)),
    )
    job = runtime.WindowJob("cam", 0, 1, time.time() * 1000)
    job.model_identity = runtime.model_identity()
    omni.concurrency = 4
    assert job.actionable
    omni.model = "second"
    assert not job.actionable


async def test_concurrent_camera_frame_offsets_do_not_accumulate_other_cameras(
    monkeypatch,
):
    import numpy as np
    from miloco.perception.engine.api import PerceptionEngine
    from miloco.perception.engine.config import PerceptionConfig
    from miloco.perception.engine.types import BatchPipelineResult
    from miloco.perception.types import BatchedSnapshot, PerceptionDevice
    from miloco.perception.utils import snapshot_from_arrays

    config = PerceptionConfig()
    config.identity_engine.enabled = False
    engine = PerceptionEngine(config)
    offsets = []

    async def pipeline(batch, *a, **kwargs):
        offsets.append((batch.snapshots[0].device.did, kwargs["frame_index_offset"]))
        return BatchPipelineResult(rooms={})

    monkeypatch.setattr(
        "miloco.perception.engine.pipeline.run_batch_pipeline", pipeline
    )
    for i, did in enumerate(["a", "b", "a", "b"]):
        job = runtime.WindowJob(did, 0, i, time.time() * 1000)
        snapshot = snapshot_from_arrays(
            PerceptionDevice(did=did, name=did, device_type="camera"),
            frames=[np.zeros((2, 2, 3), np.uint8)],
            audio=np.zeros(0, np.int16),
        )
        with runtime.window_scope(job):
            try:
                await engine.realtime_perceive(BatchedSnapshot(snapshots=[snapshot]))
            finally:
                job.finish()
    increment = config.input.fps * config.input.period_sec
    assert offsets == [("a", 0), ("b", 0), ("a", increment), ("b", increment)]


async def test_concurrent_result_keeps_payload_lifetime_until_persistence_finishes(
    monkeypatch,
):
    from types import SimpleNamespace

    from miloco.perception.client import PerceptionEngineProxy
    from miloco.perception.snapshot_context import OmniEventArtifacts
    from miloco.perception.types import RealtimePerceptionResult

    entered, release = asyncio.Event(), asyncio.Event()

    async def persist(**kwargs):
        entered.set()
        await release.wait()

    monkeypatch.setattr("miloco.perception.client._persist_meaningful_event", persist)
    monkeypatch.setattr(
        "miloco.manager.get_manager",
        lambda: SimpleNamespace(
            rule_service=SimpleNamespace(get_enabled_rule_ids=lambda: [])
        ),
    )
    proxy = PerceptionEngineProxy.__new__(PerceptionEngineProxy)
    job = runtime.WindowJob("cam", 0, 1, time.time() * 1000)
    with runtime.window_scope(job):
        task = asyncio.create_task(
            proxy.handle_realtime_perception_result(
                RealtimePerceptionResult(), artifacts=OmniEventArtifacts()
            )
        )
    await entered.wait()
    try:
        assert not task.done()
    finally:
        release.set()
        await task


async def test_on_demand_waits_for_mutable_realtime_state_to_quiesce():
    from unittest.mock import Mock

    from miloco.perception.client import PerceptionEngineProxy
    from miloco.perception.snapshot_context import OmniEventArtifacts

    proxy = PerceptionEngineProxy.__new__(PerceptionEngineProxy)
    proxy._engine_lock = asyncio.Lock()
    proxy._inference_worker = None
    proxy.perception_engine = Mock()
    proxy._concurrent_calls = {}
    proxy._closing = False
    started, release = asyncio.Event(), asyncio.Event()
    job = runtime.WindowJob("cam", 0, 1, time.time() * 1000)

    async def realtime():
        with runtime.window_scope(job):
            async with proxy._perceive_guard():
                started.set()
                await release.wait()

    active = asyncio.create_task(realtime())
    await started.wait()
    batch = Mock()
    batch.to_batched_snapshot.return_value = None
    query = asyncio.create_task(
        proxy.on_demand_perceive(batch, "test", OmniEventArtifacts())
    )
    await asyncio.sleep(0)
    try:
        assert not query.done()
        batch.to_batched_snapshot.assert_not_called()
    finally:
        release.set()
        await asyncio.gather(active, query)


async def test_http_timing_excludes_queue_and_predecessor_wait(monkeypatch):
    limiter = runtime.RequestLimiter(lambda: 1)
    monkeypatch.setattr(runtime, "HTTP_LIMITER", limiter)
    first = runtime.WindowJob("cam", 0, 1, time.time() * 1000)
    second = runtime.WindowJob("cam", 0, 2, time.time() * 1000, previous=first)

    async def finish_first():
        await asyncio.sleep(0.06)
        first.finish()

    release_first = asyncio.create_task(finish_first())

    async def second_request():
        with runtime.window_scope(second):
            async with runtime.model_request({"test": "synthetic"}):
                await asyncio.sleep(0.002)
            await second.begin_commit()
            second.finish()

    async with limiter.slot("other", 1):
        task = asyncio.create_task(second_request())
        await asyncio.sleep(0.02)
    await asyncio.gather(task, release_first)
    assert second.http_started_count == 1
    assert 1 <= second.http_ms < 15
    assert second.slot_wait_ms >= 15
    assert second.reorder_wait_ms >= 15
    assert limiter.payload_bytes == 0


async def test_local_capacity_rejection_does_not_trip_provider_circuit(monkeypatch):
    from unittest.mock import AsyncMock

    from miloco.perception.engine.config import OmniConfig
    from miloco.perception.engine.omni import omni_client

    breaker = AsyncMock()
    monkeypatch.setattr(omni_client, "get_omni_circuit_breaker", lambda: breaker)
    monkeypatch.setattr(
        runtime, "HTTP_LIMITER", runtime.RequestLimiter(lambda: 8, max_payload_bytes=0)
    )
    job = runtime.WindowJob("cam", 0, 1, time.time() * 1000)
    with runtime.window_scope(job):
        with pytest.raises(omni_client.OmniError):
            await omni_client.call_omni(
                {"system_prompt": "test", "user_content": "test"},
                OmniConfig(api_key="synthetic-test-only"),
            )
    breaker.record_failure.assert_not_called()
    assert job.http_started_count == 0


async def test_frame_reservations_stay_monotonic_across_1_8_1(monkeypatch):
    from types import SimpleNamespace

    import numpy as np
    from miloco.perception.engine.api import PerceptionEngine
    from miloco.perception.engine.config import PerceptionConfig
    from miloco.perception.engine.types import BatchPipelineResult
    from miloco.perception.types import BatchedSnapshot, PerceptionDevice
    from miloco.perception.utils import snapshot_from_arrays

    engine = PerceptionEngine(PerceptionConfig())
    # Existing non-empty identity state must survive a mere concurrency resize.
    state = SimpleNamespace(last_frame=0)
    engine._identity_engines["cam"] = SimpleNamespace(_states={1: state})
    observed = []

    async def pipeline(batch, *a, **kwargs):
        frame = kwargs.get("frame_index_offsets", {}).get(
            "cam", kwargs["frame_index_offset"]
        )
        observed.append(frame)
        state.last_frame = frame
        return BatchPipelineResult(rooms={})

    monkeypatch.setattr(
        "miloco.perception.engine.pipeline.run_batch_pipeline", pipeline
    )
    for seq, concurrent in enumerate([False, True, True, False]):
        snapshot = snapshot_from_arrays(
            PerceptionDevice(did="cam", name="cam", device_type="camera"),
            frames=[np.zeros((2, 2, 3), np.uint8)],
            audio=np.zeros(0, np.int16),
        )
        job = (
            runtime.WindowJob("cam", 0, seq, time.time() * 1000) if concurrent else None
        )
        with runtime.window_scope(job):
            try:
                await engine.realtime_perceive(BatchedSnapshot(snapshots=[snapshot]))
            finally:
                if job:
                    job.finish()
    increment = engine._config.input.fps * engine._config.input.period_sec
    assert observed == [i * increment for i in range(4)]
    assert engine._identity_engines["cam"]._states[1] is state


async def test_model_switch_during_persistence_prevents_later_agent_dispatch(
    monkeypatch,
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from miloco.perception.client import PerceptionEngineProxy
    from miloco.perception.snapshot_context import OmniEventArtifacts
    from miloco.perception.types import RealtimePerceptionResult, Suggestion

    entered, release = asyncio.Event(), asyncio.Event()
    selected = "first"
    monkeypatch.setattr(runtime, "model_identity", lambda config=None: selected)

    async def persist(**kwargs):
        entered.set()
        await release.wait()

    monkeypatch.setattr("miloco.perception.client._persist_meaningful_event", persist)
    dispatch = AsyncMock()
    monkeypatch.setattr("miloco.perception.client.dispatch_event", dispatch)
    monkeypatch.setattr(
        "miloco.manager.get_manager",
        lambda: SimpleNamespace(
            rule_service=SimpleNamespace(get_enabled_rule_ids=lambda: [])
        ),
    )
    proxy = PerceptionEngineProxy.__new__(PerceptionEngineProxy)
    job = runtime.WindowJob("cam", 0, 1, time.time() * 1000, model_identity=selected)
    with runtime.window_scope(job):
        task = asyncio.create_task(
            proxy.handle_realtime_perception_result(
                RealtimePerceptionResult(
                    suggestions=[
                        Suggestion(event="test", action="test", urgency="high")
                    ]
                ),
                artifacts=OmniEventArtifacts(),
            )
        )
    await entered.wait()
    selected = "second"
    release.set()
    await task
    dispatch.assert_not_called()


async def test_live_1_to_8_leaves_continuously_ready_legacy_drain(monkeypatch):
    from unittest.mock import Mock

    from miloco.perception.runner import PerceptionRunner

    cap = 1
    calls = 0
    collector = Mock()
    collector.get_all_active_sources.return_value = {"cam": object()}
    processor = Mock()

    async def realtime():
        nonlocal cap, calls
        calls += 1
        if calls > 2:
            raise AssertionError("legacy drain ignored live concurrency change")
        cap = 8
        return True  # The camera remains continuously ready; never return None.

    processor.process_realtime = realtime
    monkeypatch.setattr(runtime, "concurrency", lambda: cap)
    runner = PerceptionRunner(collector, processor, Mock())
    runner._is_running = True
    await runner._tick()
    assert calls == 1


async def test_post_http_window_cancel_keeps_successful_http_metrics(monkeypatch):
    from unittest.mock import Mock

    from miloco.perception.processor import PipelineProcessor
    from miloco.perception.schema import DeviceData, PerceptionBatch
    from miloco.perception.types import PerceptionDevice

    metrics = Mock()
    monkeypatch.setattr(
        "miloco.perception.processor.get_metrics_client", lambda: metrics
    )
    job = runtime.WindowJob("cam", 0, 1, time.time() * 1000)
    job.start_http(time.monotonic())
    job.end_http()
    job.trace_timing = {"gate_video_cam_pass": 1}
    batch = PerceptionBatch(
        devices={
            "cam": DeviceData(
                meta=PerceptionDevice(
                    did="cam", name="cam", device_type="camera", room_name="room"
                ),
                window_start_unix_ms=int(job.end_unix_ms - 4000),
                window_end_unix_ms=int(job.end_unix_ms),
            )
        }
    )
    processor = PipelineProcessor.__new__(PipelineProcessor)
    with runtime.window_scope(job):
        processor._publish_failed_trace(
            trace_id="test",
            cycle_start_unix_ms=int(job.end_unix_ms),
            batch=batch,
            in_delay_s=0.2,
            collect_ms=0,
            t_cycle=time.monotonic(),
            exc=asyncio.CancelledError("cancelled while persisting"),
        )
    cycle, devices = metrics.publish_trace.call_args.args
    assert cycle.omni_request_count == 1
    assert cycle.omni_request_error_count == 0
    assert devices[0].omni.error_code is None
    assert cycle.cycle_error_msg


async def test_expired_before_prepare_keeps_window_and_buffer_drop_accounting(
    monkeypatch,
):
    from unittest.mock import Mock

    from miloco.perception.processor import PipelineProcessor
    from miloco.perception.runner import PerceptionRunner
    from miloco.perception.schema import DeviceData, PerceptionBatch
    from miloco.perception.types import PerceptionDevice

    metrics = Mock()
    monkeypatch.setattr(
        "miloco.perception.processor.get_metrics_client", lambda: metrics
    )
    processor = PipelineProcessor.__new__(PipelineProcessor)
    processor._perf_enabled = True
    runner = PerceptionRunner(Mock(), processor, Mock())
    end = int(time.time() * 1000 - 181_000)
    batch = PerceptionBatch(
        devices={
            "cam": DeviceData(
                meta=PerceptionDevice(
                    did="cam", name="cam", device_type="camera", room_name="room"
                ),
                window_start_unix_ms=end - 4000,
                window_end_unix_ms=end,
                dropped_windows=4,
                partial_windows_count=1,
            )
        }
    )
    await runner._process_window(runtime.WindowJob("cam", 0, 1, end), batch)
    metrics.publish_trace.assert_called_once()
    cycle, devices = metrics.publish_trace.call_args.args
    assert cycle.device_count == 1
    assert cycle.dropped_windows_total == 4
    assert cycle.partial_windows_total == 1
    assert cycle.omni_request_count == cycle.omni_request_error_count == 0
    assert "expired_before_prepare" in cycle.cycle_error_msg
    assert devices[0].omni is None
