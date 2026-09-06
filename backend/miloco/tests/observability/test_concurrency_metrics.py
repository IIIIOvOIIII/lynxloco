import pytest
from miloco.observability.aggregate import aggregate_cycle
from miloco.observability.metrics_db import connect, init_schema
from miloco.observability.stats import drop_series, summary
from miloco.observability.types import (
    DecodeTrace,
    DeviceTraceRecord,
    GateTrace,
    OmniTrace,
)


def test_concurrent_device_time_and_attempts_are_distinct_from_batch():
    devices = [
        DeviceTraceRecord(
            device_trace_id=f"d{i}", cycle_id="c", timestamp=1000,
            device_id=f"cam{i}", room_name="room", decode=DecodeTrace(0, 0, 4, 0),
            gate=GateTrace(0, 0, 0, True, False, False), omni=OmniTrace(ms=ms),
        )
        for i, ms in enumerate((4000, 6000))
    ]
    meta = dict(
        trace_id="c", timestamp=1000, in_delay_ms=0, out_delay_ms=6000,
        decode_ms=0, collect_ms=0, convert_ms=0, log_ms=0,
        cycle_total_ms=6000, pipeline_total_ms=6000, window_duration_ms=4000,
        window_first_frame_recv_ms=None, stream_lag_ms=None,
    )
    record = aggregate_cycle(devices, meta)
    assert record.omni_ms == 10000
    assert record.omni_wall_ms == 6000
    assert record.omni_call_count == 1
    assert record.omni_request_count == 2
    assert record.metric_version == 2


def test_summary_uses_camera_denominator_and_excludes_gate_only_cycles(tmp_path):
    conn = connect(tmp_path / "obs.db")
    init_schema(conn)
    conn.execute(
        "INSERT INTO traces (trace_id,timestamp,device_count,skipped,"
        "cycle_total_ms,window_duration_ms,omni_ms,omni_wall_ms,omni_call_count,"
        "omni_request_count,dropped_windows_total,metric_version) "
        "VALUES ('model',1000,2,0,6000,4000,10000,6000,1,2,2,2)"
    )
    for i in range(30):
        conn.execute(
            "INSERT INTO traces (trace_id,timestamp,device_count,skipped,"
            "cycle_total_ms,window_duration_ms,omni_call_count,metric_version) "
            "VALUES (?,1001,1,1,1,4000,0,2)", (f"gate-{i}",),
        )
    result = summary(conn, "1h", 1, 2000)
    assert result["camera_window_count"] == 32
    assert result["expected_window_count"] == 34
    assert result["drop_rate"] == pytest.approx(2 / 34)
    assert result["p95_rtf_e2e"] == 1.5
    assert result["p95_rtf_omni"] == 1.5
    assert result["omni_success_cycle_count"] == 1
    assert result["omni_request_count"] == 2
    conn.close()


def test_legacy_drop_counts_are_not_presented_as_corrected_coverage(tmp_path):
    conn = connect(tmp_path / "obs.db")
    init_schema(conn)
    conn.execute(
        "INSERT INTO traces (trace_id,timestamp,device_count,dropped_windows_total) "
        "VALUES ('old',1000,2,9)"
    )
    result = summary(conn, "1h", 1, 2000)
    assert result["legacy_cycle_count"] == 1
    assert result["drop_rate"] is None
    assert result["dropped_count"] == 0
    assert drop_series(conn, "1h", 1, 2000) == []
    conn.close()


def test_v4_migration_preserves_rows_and_marks_old_metric_definition(tmp_path):
    conn = connect(tmp_path / "old.db")
    init_schema(conn)
    conn.execute("INSERT INTO traces (trace_id,timestamp) VALUES ('old',1000)")
    conn.execute("DROP VIEW traces_v")
    for column in ("omni_wall_ms", "omni_request_count", "omni_request_error_count",
                   "partial_windows_total", "metric_version"):
        conn.execute(f"ALTER TABLE traces DROP COLUMN {column}")
    conn.execute("ALTER TABLE traces_device DROP COLUMN partial_windows_count")
    conn.execute("PRAGMA user_version=4")
    init_schema(conn)
    init_schema(conn)
    assert conn.execute("SELECT metric_version FROM traces WHERE trace_id='old'").fetchone() == (1,)
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 5
    conn.close()


def test_http_success_with_cancelled_window_is_not_a_completed_success_cycle(tmp_path):
    conn = connect(tmp_path / "obs.db")
    init_schema(conn)
    conn.execute(
        "INSERT INTO traces (trace_id,timestamp,device_count,cycle_total_ms,"
        "window_duration_ms,omni_wall_ms,omni_call_count,omni_request_count,"
        "cycle_error_msg,metric_version) "
        "VALUES ('cancel-after-http',1000,1,6000,4000,1000,1,1,'CancelledError',2)"
    )
    result = summary(conn, "1h", 1, 2000)
    assert result["omni_request_count"] == 1
    assert result["omni_request_error_count"] == 0
    assert result["omni_success_cycle_count"] == 0
    assert result["p95_rtf_e2e"] == 0
    conn.close()
