import logging

import miloco.auth.service as service_module
from fastapi.testclient import TestClient
from miloco.config import reset_settings
from miloco.database.connector import init_database
from miloco.main import app


def _reset_database_connector() -> None:
    import miloco.database.connector as connector_module

    connector_module.db_connector = None


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(tmp_path / "miloco.db"))
    monkeypatch.setenv("MILOCO_SERVER__TOKEN", "service-token")
    _reset_database_connector()
    reset_settings()
    init_database()
    monkeypatch.setattr(
        service_module, "_LOGIN_ATTEMPT_THROTTLE", service_module.LoginAttemptThrottle()
    )
    return TestClient(app)


def _setup(client: TestClient) -> dict:
    response = client.post(
        "/api/auth/setup",
        json={
            "username": "lynx",
            "display_name": "Lynx",
            "password": "correct horse battery",
            "password_confirm": "correct horse battery",
        },
    )
    assert response.status_code == 200
    return response.json()["data"]


def _service_client() -> TestClient:
    return TestClient(app, headers={"Authorization": "Bearer service-token"})


def test_auth_status_requires_setup_on_fresh_db(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    response = client.get("/api/auth/status")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data == {
        "needs_setup": True,
        "authenticated": False,
        "user": None,
        "csrf_token": None,
    }


def test_setup_creates_first_admin_and_sets_cookie(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    data = _setup(client)

    assert client.cookies.get("miloco_dashboard_session")
    assert data["needs_setup"] is False
    assert data["authenticated"] is True
    assert data["user"]["username"] == "lynx"
    assert data["csrf_token"]

    second = client.post(
        "/api/auth/setup",
        json={
            "username": "other",
            "display_name": "Other",
            "password": "correct horse battery",
            "password_confirm": "correct horse battery",
        },
    )
    assert second.status_code in {400, 409}


def test_login_failure_is_generic_and_sets_no_cookie(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    _setup(client)

    response = TestClient(app).post(
        "/api/auth/login",
        json={"username": "lynx", "password": "wrong password"},
    )

    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    body = response.json()
    assert body["code"] == 1003
    assert "password_hash" not in response.text


def test_login_success_sets_cookie_and_returns_identity(tmp_path, monkeypatch) -> None:
    setup_client = _client(tmp_path, monkeypatch)
    _setup(setup_client)
    client = TestClient(app)

    response = client.post(
        "/api/auth/login",
        json={"username": "lynx", "password": "correct horse battery"},
    )

    assert response.status_code == 200
    assert client.cookies.get("miloco_dashboard_session")
    data = response.json()["data"]
    assert data["authenticated"] is True
    assert data["user"]["username"] == "lynx"
    assert data["csrf_token"]


def test_public_auth_rejects_oversized_credentials_without_echoing_password(
    tmp_path, monkeypatch, caplog
) -> None:
    client = _client(tmp_path, monkeypatch)
    oversized_password = "p" * 257

    with caplog.at_level(logging.WARNING):
        password_response = client.post(
            "/api/auth/login",
            json={"username": "lynx", "password": oversized_password},
        )
        username_response = client.post(
            "/api/auth/login",
            json={"username": "u" * 129, "password": "valid-length"},
        )

    assert password_response.status_code == 422
    assert username_response.status_code == 422
    assert oversized_password not in password_response.text
    assert oversized_password not in caplog.text


def test_custom_validator_error_returns_redacted_json_422(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    raw_username = "   "

    response = client.post(
        "/api/auth/setup",
        json={
            "username": raw_username,
            "display_name": "Lynx",
            "password": "correct horse battery",
            "password_confirm": "correct horse battery",
        },
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["code"] == 1002
    assert raw_username not in response.text


def test_logout_requires_csrf_for_valid_session_then_remains_anonymous_idempotent(
    tmp_path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    setup_data = _setup(client)

    missing_csrf = client.post("/api/auth/logout")
    response = client.post(
        "/api/auth/logout",
        headers={"X-Miloco-CSRF": setup_data["csrf_token"]},
    )
    second = client.post("/api/auth/logout")

    assert missing_csrf.status_code == 403
    assert response.status_code == 200
    assert "miloco_dashboard_session=" in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert second.status_code == 200


def test_password_change_revokes_existing_dashboard_session(tmp_path, monkeypatch) -> None:
    dashboard_client = _client(tmp_path, monkeypatch)
    setup_data = _setup(dashboard_client)

    changed = _service_client().post(
        f"/api/users/{setup_data['user']['id']}/password",
        json={
            "password": "new correct horse battery",
            "password_confirm": "new correct horse battery",
        },
    )
    old_session = dashboard_client.get("/api/auth/me")

    assert changed.status_code == 200
    assert old_session.status_code == 401


def test_password_change_rejects_old_password_and_accepts_new_password(
    tmp_path, monkeypatch
) -> None:
    dashboard_client = _client(tmp_path, monkeypatch)
    setup_data = _setup(dashboard_client)
    user_id = setup_data["user"]["id"]

    changed = _service_client().post(
        f"/api/users/{user_id}/password",
        json={
            "password": "new correct horse battery",
            "password_confirm": "new correct horse battery",
        },
    )
    old_login = TestClient(app).post(
        "/api/auth/login",
        json={"username": "lynx", "password": "correct horse battery"},
    )
    new_login = TestClient(app).post(
        "/api/auth/login",
        json={"username": "lynx", "password": "new correct horse battery"},
    )

    assert changed.status_code == 200
    assert old_login.status_code == 401
    assert new_login.status_code == 200


def test_authenticated_status_recovers_stable_csrf_without_invalidating_writes(
    tmp_path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    setup_data = _setup(client)

    first_tab_status = client.get("/api/auth/status")
    second_tab_me = client.get("/api/auth/me")
    write_with_first_tab_token = client.post(
        "/api/users",
        headers={"X-Miloco-CSRF": setup_data["csrf_token"]},
        json={
            "username": "operator",
            "display_name": "Operator",
            "password": "correct horse battery",
            "password_confirm": "correct horse battery",
        },
    )

    assert first_tab_status.status_code == 200
    assert second_tab_me.status_code == 200
    assert write_with_first_tab_token.status_code == 200
    data = second_tab_me.json()["data"]
    assert data["authenticated"] is True
    assert data["user"]["username"] == "lynx"
    assert first_tab_status.json()["data"]["csrf_token"] == setup_data["csrf_token"]
    assert data["csrf_token"] == setup_data["csrf_token"]
    for forbidden in (
        "password_hash",
        "session_hash",
        "csrf_hash",
        "service-token",
    ):
        assert forbidden not in second_tab_me.text


def test_service_token_remains_compatible_with_auth_me(tmp_path, monkeypatch) -> None:
    _client(tmp_path, monkeypatch)

    response = _service_client().get("/api/auth/me")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "needs_setup": True,
        "authenticated": False,
        "user": None,
        "csrf_token": None,
    }


def test_service_token_and_dashboard_session_can_list_users(
    tmp_path, monkeypatch
) -> None:
    dashboard_client = _client(tmp_path, monkeypatch)
    _setup(dashboard_client)
    service_client = _service_client()

    listed_with_session = dashboard_client.get("/api/users")
    created = service_client.post(
        "/api/users",
        json={
            "username": "operator",
            "display_name": "Operator",
            "password": "correct horse battery",
            "password_confirm": "correct horse battery",
        },
    )
    listed = service_client.get("/api/users")

    assert listed_with_session.status_code == 200
    assert created.status_code == 200
    assert created.json()["data"]["username"] == "operator"
    assert listed.status_code == 200
    users = listed.json()["data"]["users"]
    assert {user["username"] for user in users} == {"lynx", "operator"}
    assert all("password_hash" not in user for user in users)


def test_user_management_enforces_last_admin_and_current_session_safety(
    tmp_path, monkeypatch
) -> None:
    dashboard_client = _client(tmp_path, monkeypatch)
    setup_data = _setup(dashboard_client)
    service_client = _service_client()
    admin_id = setup_data["user"]["id"]
    created = service_client.post(
        "/api/users",
        json={
            "username": "operator",
            "display_name": "Operator",
            "password": "correct horse battery",
            "password_confirm": "correct horse battery",
        },
    )
    operator_id = created.json()["data"]["id"]

    disable_operator = service_client.patch(
        f"/api/users/{operator_id}", json={"enabled": False}
    )
    last_admin_disable = service_client.patch(
        f"/api/users/{admin_id}", json={"enabled": False}
    )
    delete_last_admin = service_client.delete(f"/api/users/{admin_id}")
    password_change = service_client.post(
        f"/api/users/{operator_id}/password",
        json={
            "password": "new correct horse battery",
            "password_confirm": "new correct horse battery",
        },
    )

    assert last_admin_disable.status_code in {400, 409}
    assert disable_operator.status_code == 200
    assert delete_last_admin.status_code in {400, 409}
    assert password_change.status_code == 200


def test_dashboard_session_cannot_delete_its_current_user(tmp_path, monkeypatch) -> None:
    dashboard_client = _client(tmp_path, monkeypatch)
    setup_data = _setup(dashboard_client)
    admin_id = setup_data["user"]["id"]
    _service_client().post(
        "/api/users",
        json={
            "username": "operator",
            "display_name": "Operator",
            "password": "correct horse battery",
            "password_confirm": "correct horse battery",
        },
    )

    response = dashboard_client.delete(
        f"/api/users/{admin_id}",
        headers={"X-Miloco-CSRF": setup_data["csrf_token"]},
    )

    assert response.status_code == 409
    assert response.json()["message"] == "Current user cannot be deleted"
