from pathlib import Path

from fastapi.testclient import TestClient
from miloco.config import reset_settings
from miloco.database.connector import init_database
from miloco.main import app


def _reset_database_connector() -> None:
    import miloco.database.connector as connector_module

    connector_module.db_connector = None


def test_shipped_spa_template_has_no_token_injection_marker() -> None:
    """The browser bundle itself must not retain the retired token hook."""
    template = (Path(__file__).resolve().parents[4] / "web" / "index.html").read_text(
        encoding="utf-8"
    )

    assert "__MILOCO_INJECT_TOKEN_HERE__" not in template
    assert "window.__MILOCO_TOKEN__" not in template


def test_spa_html_never_injects_service_token(tmp_path, monkeypatch) -> None:
    """A browser shell must not receive machine credentials on either SPA URL."""
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(
        '<script>window.__MILOCO_TOKEN__ = "__MILOCO_INJECT_TOKEN_HERE__";</script>',
        encoding="utf-8",
    )
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(tmp_path / "miloco.db"))
    monkeypatch.setenv("MILOCO_SERVER__TOKEN", "service-token-secret")
    monkeypatch.setenv("MILOCO_DIRECTORIES__STATIC", str(static_dir))
    _reset_database_connector()
    reset_settings()
    init_database()

    client = TestClient(app)
    for path in ("/", "/index.html"):
        response = client.get(path)

        assert response.status_code == 200
        assert "service-token-secret" not in response.text
        assert "__MILOCO_INJECT_TOKEN_HERE__" not in response.text
