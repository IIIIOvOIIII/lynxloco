from collections.abc import Callable

import miloco.auth.service as service_module
import pytest
from fastapi import Request, Response
from miloco.auth.passwords import hash_password
from miloco.auth.repo import DashboardAuthRepo
from miloco.auth.schema import LoginRequest
from miloco.auth.service import AuthService
from miloco.config import reset_settings
from miloco.database.connector import init_database
from miloco.middleware.exceptions import AuthenticationException


def _reset_database_connector() -> None:
    import miloco.database.connector as connector_module

    connector_module.db_connector = None


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/login",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )


@pytest.mark.parametrize(
    ("username", "prepare"),
    [
        ("missing", lambda repo: None),
        (
            "disabled",
            lambda repo: repo.update_user(
                repo.create_user(
                    "disabled", "Disabled", hash_password("correct horse battery")
                ).id,
                enabled=False,
            ),
        ),
        (
            "enabled",
            lambda repo: repo.create_user(
                "enabled", "Enabled", hash_password("correct horse battery")
            ),
        ),
    ],
)
def test_login_verifies_password_for_every_failed_identity_case(
    tmp_path,
    monkeypatch,
    username: str,
    prepare: Callable[[DashboardAuthRepo], object],
) -> None:
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(tmp_path / "miloco.db"))
    _reset_database_connector()
    reset_settings()
    init_database()
    repo = DashboardAuthRepo()
    prepare(repo)

    calls: list[tuple[str, str]] = []

    def _verify(password: str, password_hash: str) -> bool:
        calls.append((password, password_hash))
        return False

    monkeypatch.setattr(service_module, "verify_password", _verify)

    with pytest.raises(AuthenticationException, match="Invalid username or password"):
        AuthService(repo).login(
            LoginRequest(username=username, password="wrong password"),
            _request(),
            Response(),
        )

    assert len(calls) == 1
    assert calls[0][0] == "wrong password"
