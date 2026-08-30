from collections.abc import Callable

import miloco.auth.service as service_module
import pytest
from fastapi import Request, Response
from miloco.auth.passwords import hash_password
from miloco.auth.repo import DashboardAuthRepo
from miloco.auth.schema import LoginRequest, SetupRequest
from miloco.auth.service import AuthService
from miloco.config import reset_settings
from miloco.database.connector import init_database
from miloco.middleware.exceptions import AuthenticationException


def _reset_database_connector() -> None:
    import miloco.database.connector as connector_module

    connector_module.db_connector = None


def _request(client_ip: str = "127.0.0.1") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/login",
            "headers": [],
            "client": (client_ip, 12345),
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


def test_setup_completed_is_rejected_before_password_hashing(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(tmp_path / "miloco.db"))
    _reset_database_connector()
    reset_settings()
    init_database()
    repo = DashboardAuthRepo()
    repo.create_user("existing", "Existing", hash_password("correct horse battery"))

    def _unexpected_hash(_password: str) -> str:
        raise AssertionError("completed setup must not hash another password")

    monkeypatch.setattr(service_module, "hash_password", _unexpected_hash)

    with pytest.raises(service_module.HTTPException) as exc_info:
        AuthService(repo).setup_first_admin(
            SetupRequest(
                username="other",
                display_name="Other",
                password="another valid password",
                password_confirm="another valid password",
            ),
            _request(),
            Response(),
        )

    assert exc_info.value.http_status == 409


def test_login_throttle_blocks_repeated_account_attempts_across_sources(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(tmp_path / "miloco.db"))
    _reset_database_connector()
    reset_settings()
    init_database()
    throttle = service_module.LoginAttemptThrottle(
        account_limit=2, source_limit=10, window_seconds=300
    )
    calls = 0

    def _verify(_password: str, _password_hash: str) -> bool:
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr(service_module, "verify_password", _verify)
    service = AuthService(DashboardAuthRepo(), login_throttle=throttle)
    body = LoginRequest(username="target", password="wrong password")

    messages = []
    for client_ip in ("192.0.2.1", "192.0.2.2", "192.0.2.3"):
        with pytest.raises(AuthenticationException) as exc_info:
            service.login(body, _request(client_ip), Response())
        messages.append(exc_info.value.message)

    assert calls == 2
    assert messages == ["Invalid username or password"] * 3


def test_login_throttle_blocks_repeated_source_attempts_across_accounts(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(tmp_path / "miloco.db"))
    _reset_database_connector()
    reset_settings()
    init_database()
    throttle = service_module.LoginAttemptThrottle(
        account_limit=10, source_limit=2, window_seconds=300
    )
    calls = 0

    def _verify(_password: str, _password_hash: str) -> bool:
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr(service_module, "verify_password", _verify)
    service = AuthService(DashboardAuthRepo(), login_throttle=throttle)

    messages = []
    for username in ("first", "second", "third"):
        with pytest.raises(AuthenticationException) as exc_info:
            service.login(
                LoginRequest(username=username, password="wrong password"),
                _request("192.0.2.1"),
                Response(),
            )
        messages.append(exc_info.value.message)

    assert calls == 2
    assert messages == ["Invalid username or password"] * 3
