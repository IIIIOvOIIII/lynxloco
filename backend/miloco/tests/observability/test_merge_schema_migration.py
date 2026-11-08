"""Upgrades from the independently released fork and upstream schemas."""

import pytest
from miloco.observability.metrics_db import SCHEMA_VERSION, connect, init_schema

CONCURRENCY_COLUMNS = (
    "omni_wall_ms", "omni_request_count", "omni_request_error_count",
    "partial_windows_total", "metric_version",
)


def test_fork_v5_gains_ledger_columns_without_losing_concurrency(tmp_path):
    conn = connect(tmp_path / "fork-v5.db")
    init_schema(conn)
    conn.execute("DROP INDEX idx_action_ledger_trigger")
    for column in ("phase", "trigger_event_id", "auth_state"):
        conn.execute(f"ALTER TABLE action_ledger DROP COLUMN {column}")
    conn.execute("PRAGMA user_version=5")
    conn.execute(
        "INSERT INTO traces (trace_id,timestamp,omni_wall_ms,omni_request_count,"
        "partial_windows_total,metric_version) VALUES ('cycle',1,1200,2,3,2)"
    )
    conn.execute(
        "INSERT INTO action_ledger (id,timestamp,action_type,did,success) "
        "VALUES ('old',1,'set_property','d1',1)"
    )

    init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute(
        "SELECT omni_wall_ms,omni_request_count,partial_windows_total,metric_version "
        "FROM traces WHERE trace_id='cycle'"
    ).fetchone() == (1200, 2, 3, 2)
    assert conn.execute(
        "SELECT phase,trigger_event_id,auth_state FROM action_ledger WHERE id='old'"
    ).fetchone() == ("legacy", None, None)
    conn.execute(
        "INSERT INTO action_ledger (id,timestamp,action_type,did,success) "
        "VALUES ('manual',2,'set_property','d1',1)"
    )
    init_schema(conn)
    assert conn.execute(
        "SELECT phase FROM action_ledger WHERE id='manual'"
    ).fetchone() == (None,)
    conn.close()


@pytest.mark.parametrize("version", [5, 6])
def test_upstream_schema_gains_concurrency_without_remarking_ledger(tmp_path, version):
    conn = connect(tmp_path / f"upstream-v{version}.db")
    init_schema(conn)
    conn.execute("DROP VIEW traces_v")
    for column in CONCURRENCY_COLUMNS:
        conn.execute(f"ALTER TABLE traces DROP COLUMN {column}")
    conn.execute("ALTER TABLE traces_device DROP COLUMN partial_windows_count")
    if version == 5:
        conn.execute("ALTER TABLE action_ledger DROP COLUMN auth_state")
    conn.execute(f"PRAGMA user_version={version}")
    conn.execute("INSERT INTO traces (trace_id,timestamp) VALUES ('old',1)")
    for action_id, phase, event_id in (
        ("manual", None, None), ("linked", "enter", "event"), ("old", "legacy", None),
    ):
        conn.execute(
            "INSERT INTO action_ledger (id,timestamp,action_type,did,success,"
            "phase,trigger_event_id) VALUES (?,1,'set_property','d1',1,?,?)",
            (action_id, phase, event_id),
        )
    if version == 6:
        conn.execute("UPDATE action_ledger SET auth_state='degraded' WHERE id='linked'")

    init_schema(conn)
    init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute(
        "SELECT omni_wall_ms,omni_request_count,omni_request_error_count,"
        "partial_windows_total,metric_version FROM traces WHERE trace_id='old'"
    ).fetchone() == (None, 0, 0, 0, 1)
    assert "partial_windows_count" in {
        row[1] for row in conn.execute("PRAGMA table_info(traces_device)")
    }
    assert conn.execute(
        "SELECT id,phase,trigger_event_id,auth_state FROM action_ledger ORDER BY id"
    ).fetchall() == [
        ("linked", "enter", "event", "degraded" if version == 6 else None),
        ("manual", None, None, None),
        ("old", "legacy", None, None),
    ]
    assert conn.execute("SELECT rtf_omni_wall FROM traces_v").fetchone() == (None,)
    conn.close()
