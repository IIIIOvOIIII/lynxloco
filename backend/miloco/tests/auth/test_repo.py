import pytest
from miloco.auth.passwords import hash_password
from miloco.auth.repo import DashboardAuthRepo
from miloco.config import reset_settings
from miloco.database.connector import init_database


def _reset_database_connector() -> None:
    import miloco.database.connector as connector_module

    connector_module.db_connector = None


def test_user_crud_never_returns_password_hash_in_public_model(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(tmp_path / "miloco.db"))
    _reset_database_connector()
    reset_settings()
    init_database()

    repo = DashboardAuthRepo()
    user = repo.create_user(
        username="lynx",
        display_name="Lynx",
        password_hash=hash_password("correct horse battery"),
    )

    assert user.username == "lynx"
    assert user.username_norm == "lynx"
    assert repo.any_user_exists() is True
    assert repo.any_enabled_user() is True
    assert repo.get_user_by_username("LYNX") is not None
    public = user.to_public()
    assert public.model_dump() == {
        "id": user.id,
        "username": "lynx",
        "display_name": "Lynx",
        "role": "admin",
        "enabled": True,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
        "last_login_at": None,
    }
    assert "password_hash" not in public.model_dump()


def test_session_lookup_uses_hash_and_expiry(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(tmp_path / "miloco.db"))
    _reset_database_connector()
    reset_settings()
    init_database()

    repo = DashboardAuthRepo()
    user = repo.create_user("lynx", "Lynx", hash_password("correct horse battery"))
    session = repo.create_session(
        user_id=user.id,
        session_hash="hash-session",
        csrf_hash="hash-csrf",
        expires_at=2_000,
        user_agent_hash="ua",
        client_ip_hint="127.0.0.1",
    )

    assert repo.get_session_by_hash("hash-session", now_ms=1_000).id == session.id
    assert repo.get_session_by_hash("hash-session", now_ms=2_001) is None


def test_create_user_rejects_non_admin_role_without_persisting_it(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(tmp_path / "miloco.db"))
    _reset_database_connector()
    reset_settings()
    init_database()

    repo = DashboardAuthRepo()

    with pytest.raises(ValueError):
        repo.create_user(
            username="lynx",
            display_name="Lynx",
            password_hash=hash_password("correct horse battery"),
            role="viewer",
        )

    assert repo.any_user_exists() is False
