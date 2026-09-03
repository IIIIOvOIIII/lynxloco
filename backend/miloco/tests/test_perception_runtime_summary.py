"""Runtime summary helpers for perception dashboard state."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))

    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module

    monkeypatch.setattr(connector_module, "db_connector", None)
    connector_module.init_database()
    yield db_file
    reset_settings()


@pytest.fixture
def meaningful_dao(real_db):
    from miloco.database.meaningful_events_dao import MeaningfulEventDao

    return MeaningfulEventDao()


def _insert_meaningful(dao, *, timestamp: int) -> str:
    event_id = str(uuid.uuid4())
    assert dao.insert(
        event_id=event_id,
        timestamp=timestamp,
        text="有人经过",
        payload_json="{}",
        has_rule_hit=False,
        has_suggestion=True,
        has_asr=False,
        device_ids=["rtsp:living"],
    )
    return event_id


def test_runtime_summary_classifies_silent_when_cycles_run_but_semantics_empty():
    from miloco.perception.service import classify_perception_runtime_state

    state, hints = classify_perception_runtime_state(
        running=True,
        ready=True,
        active_source_count=2,
        raw_last_hour=0,
        meaningful_last_hour=0,
        consecutive_empty_descriptions=5,
        recent_window={
            "cycle_count": 10,
            "omni_call_count": 10,
            "omni_error_count": 0,
            "cycle_error_count": 0,
        },
    )

    assert state == "silent"
    assert "semantic_output_empty" in hints
    assert "raw_logs_deduplicated" in hints


def test_meaningful_event_counts_since_and_latest_timestamp(meaningful_dao):
    _insert_meaningful(meaningful_dao, timestamp=1000)
    _insert_meaningful(meaningful_dao, timestamp=2500)

    assert meaningful_dao.count_all() == 2
    assert meaningful_dao.count_since(2000) == 1
    assert meaningful_dao.latest_timestamp_ms() == 2500


def test_query_observability_windows_aggregates_recent_traces(tmp_path):
    from miloco.observability.metrics_db import connect, init_schema
    from miloco.perception.service import PerceptionService

    db_path = tmp_path / "obs.sqlite"
    with connect(db_path) as conn:
        init_schema(conn)
        conn.execute(
            """
            INSERT INTO traces (
              trace_id, timestamp, skipped, gate_video_pass, gate_audio_pass,
              gate_hold_pass, omni_call_count, omni_error_count,
              cycle_error_msg, dropped_windows_total, overflow_count_total
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("recent", 1_000_000, 0, 1, 0, 1, 2, 1, "", 4, 1),
        )
        conn.execute(
            """
            INSERT INTO traces (
              trace_id, timestamp, skipped, gate_video_pass, gate_audio_pass,
              gate_hold_pass, omni_call_count, omni_error_count,
              cycle_error_msg, dropped_windows_total, overflow_count_total
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("old", 1_000_000 - 20 * 60_000, 1, 0, 1, 0, 1, 0, "boom", 9, 3),
        )

    service = PerceptionService.__new__(PerceptionService)
    windows = service._query_observability_windows(
        obs_db_path=db_path,
        now_ms=1_000_000,
        windows_minutes=(5, 15, 60),
    )

    by_minutes = {window["minutes"]: window for window in windows}
    assert by_minutes[5]["cycle_count"] == 1
    assert by_minutes[5]["video_pass_count"] == 1
    assert by_minutes[5]["omni_call_count"] == 2
    assert by_minutes[5]["omni_error_count"] == 1
    assert by_minutes[5]["dropped_windows_count"] == 4
    assert by_minutes[60]["cycle_count"] == 2
    assert by_minutes[60]["skipped_count"] == 1
    assert by_minutes[60]["cycle_error_count"] == 1
    assert by_minutes[60]["overflow_count"] == 3


def test_runtime_summary_returns_silent_state_with_sanitized_omni(
    tmp_path, real_db, meaningful_dao
):
    from miloco.database.perception_repo import PerceptionLogRepo
    from miloco.observability.metrics_db import connect, init_schema
    from miloco.perception.runtime_diagnostics import (
        RealtimeOmniDiagnostic,
        get_runtime_diagnostics,
    )
    from miloco.perception.schema import (
        EngineState,
        PerceptionEngineStatus,
        PerceptionLogEntry,
    )
    from miloco.perception.service import PerceptionService
    from miloco.perception.types import PerceptionDevice

    now_ms_value = 10_000_000
    log_repo = PerceptionLogRepo()
    assert log_repo.append(PerceptionLogEntry(id="p1", timestamp=1_000, descriptions={}))
    assert not log_repo.append(
        PerceptionLogEntry(id="p2", timestamp=now_ms_value - 20_000, descriptions={})
    )
    assert not log_repo.append(
        PerceptionLogEntry(id="p3", timestamp=now_ms_value - 10_000, descriptions={})
    )

    obs_db = tmp_path / "obs.sqlite"
    with connect(obs_db) as conn:
        init_schema(conn)
        conn.execute(
            """
            INSERT INTO traces (
              trace_id, timestamp, skipped, gate_video_pass, gate_audio_pass,
              gate_hold_pass, omni_call_count, omni_error_count,
              cycle_error_msg, dropped_windows_total, overflow_count_total
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("recent", now_ms_value - 1_000, 0, 1, 0, 1, 3, 0, "", 2, 0),
        )

    diagnostics = get_runtime_diagnostics()
    diagnostics.reset_for_tests()
    diagnostics.record(
        RealtimeOmniDiagnostic(
            timestamp_ms=now_ms_value - 500,
            protocol="openai_responses",
            route="realtime",
            message_count=2,
            text_block_count=5,
            image_block_count=1,
            video_block_count=0,
            audio_block_count=0,
            response_text_length=18,
            response_json_like=True,
            parse_ok=True,
            skipped=False,
            caption_count=0,
            matched_rule_count=0,
            suggestion_count=0,
            speech_count=0,
            complete_speech_count=0,
            needs_response_speech_count=0,
        )
    )

    service = PerceptionService.__new__(PerceptionService)
    service._log_repo = log_repo
    service._meaningful_events_dao = meaningful_dao
    service._engine = SimpleNamespace(
        status=lambda: PerceptionEngineStatus(
            running=True,
            engine=EngineState(ready=True, status="ready", message=""),
            active_sources=[
                {
                    "did": "rtsp:living",
                    "name": "客厅摄像头",
                    "device_type": "camera",
                    "modalities": ["video"],
                },
                {
                    "did": "rtsp:kitchen",
                    "name": "厨房摄像头",
                    "device_type": "camera",
                    "modalities": ["video"],
                },
            ],
        )
    )
    service._collector = SimpleNamespace(
        get_all_active_sources=lambda: {
            "rtsp:living": PerceptionDevice(
                did="rtsp:living", name="客厅摄像头", device_type="camera"
            ),
            "rtsp:kitchen": PerceptionDevice(
                did="rtsp:kitchen", name="厨房摄像头", device_type="camera"
            ),
        }
    )

    summary = service.runtime_summary(obs_db_path=obs_db, now_ms_value=now_ms_value)

    assert summary.engine.running is True
    assert summary.sources.active_count == 2
    assert summary.logs.today_inference_count == 3
    assert summary.logs.raw_last_hour == 0
    assert summary.logs.meaningful_last_hour == 0
    assert {window.minutes for window in summary.windows} == {5, 15, 60}
    assert summary.semantic_state == "silent"
    assert "semantic_output_empty" in summary.hints
    assert summary.latest_omni is not None
    assert summary.latest_omni.request["image_block_count"] == 1
    assert summary.latest_omni.response["classification"] == "semantic_empty"


def test_runtime_summary_router_returns_standard_envelope(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from miloco.perception import router as perception_router

    captured = {}

    class _Service:
        def runtime_summary(self, *, obs_db_path=None):
            captured["obs_db_path"] = obs_db_path
            return {
                "now_ms": 10,
                "engine": {"running": True, "ready": True, "status": "ready", "message": ""},
                "sources": {"active_count": 2, "active_sources": []},
                "logs": {
                    "today_inference_count": 1,
                    "raw_total": 0,
                    "raw_last_hour": 0,
                    "last_inference_ms": 9,
                    "last_insert_ms": None,
                    "last_descriptions_empty": True,
                    "last_append_inserted": False,
                    "consecutive_empty_descriptions": 3,
                    "consecutive_deduplicated": 2,
                    "meaningful_total": 0,
                    "meaningful_last_hour": 0,
                    "last_meaningful_event_ms": None,
                },
                "windows": [{"minutes": 5}, {"minutes": 15}, {"minutes": 60}],
                "latest_omni": None,
                "semantic_state": "silent",
                "hints": ["semantic_output_empty"],
            }

    monkeypatch.setattr(
        perception_router.manager,
        "_perception_service",
        _Service(),
        raising=False,
    )

    app = FastAPI()
    app.state.obs_db_path = "/tmp/obs.sqlite"
    app.include_router(perception_router.router, prefix="/api")

    response = TestClient(app).get("/api/perception/runtime-summary")

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    data = payload["data"]
    assert data["engine"]["running"] is True
    assert data["sources"]["active_count"] == 2
    assert data["logs"]["today_inference_count"] >= 1
    assert {window["minutes"] for window in data["windows"]} == {5, 15, 60}
    assert data["semantic_state"] == "silent"
    assert "semantic_output_empty" in data["hints"]
    assert captured["obs_db_path"] == "/tmp/obs.sqlite"
