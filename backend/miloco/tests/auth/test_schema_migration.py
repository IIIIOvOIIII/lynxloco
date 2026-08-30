import sqlite3

from miloco.config import reset_settings
from miloco.database.connector import init_database


def _reset_database_connector() -> None:
    import miloco.database.connector as connector_module

    connector_module.db_connector = None


def test_fresh_database_creates_dashboard_auth_tables(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "miloco.db"
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_path))
    _reset_database_connector()
    reset_settings()

    init_database()

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "dashboard_user" in tables
        assert "dashboard_session" in tables
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
