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


def test_logout_clears_session_cookie_and_is_idempotent(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    _setup(client)

    response = client.post("/api/auth/logout")
    second = client.post("/api/auth/logout")

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


def test_authenticated_me_rotates_csrf_without_sensitive_fields(
    tmp_path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    setup_data = _setup(client)

    response = client.get("/api/auth/me")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["authenticated"] is True
    assert data["user"]["username"] == "lynx"
    assert data["csrf_token"]
    assert data["csrf_token"] != setup_data["csrf_token"]
    for forbidden in (
        "password_hash",
        "session_hash",
        "csrf_hash",
        "service-token",
    ):
        assert forbidden not in response.text


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


def test_service_token_can_manage_users_and_session_cannot_yet(
    tmp_path, monkeypatch
) -> None:
    dashboard_client = _client(tmp_path, monkeypatch)
    _setup(dashboard_client)
    service_client = _service_client()

    denied = dashboard_client.get("/api/users")
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

    assert denied.status_code == 401
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
    delete_current = service_client.delete(f"/api/users/{admin_id}")
    password_change = service_client.post(
        f"/api/users/{operator_id}/password",
        json={
            "password": "new correct horse battery",
            "password_confirm": "new correct horse battery",
        },
    )

    assert last_admin_disable.status_code in {400, 409}
    assert disable_operator.status_code == 200
    assert delete_current.status_code in {400, 409}
    assert password_change.status_code == 200
