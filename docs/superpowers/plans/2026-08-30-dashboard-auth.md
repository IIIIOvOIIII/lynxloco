# Dashboard Authentication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build native Miloco dashboard authentication with first-admin setup, login/logout, session-cookie dashboard access, CSRF protection, and user management while preserving service-token access for CLI/OpenClaw/Hermes.

**Architecture:** Add a focused backend auth package backed by SQLite users and hashed server-side sessions. Keep the existing `server.token` machine lane, but stop injecting it into dashboard HTML; browsers authenticate with HttpOnly cookies and CSRF tokens. Add a frontend auth gate before the existing dashboard shell and a Users tab for local account administration.

**Tech Stack:** FastAPI, SQLite, `argon2-cffi`, `secrets`, `hashlib`, `hmac`, React 19, TypeScript, i18next, Vitest, pytest, Click CLI.

**Spec:** `docs/superpowers/specs/2026-08-30-dashboard-auth-design.md`

## Global Constraints

- No unauthenticated browser may receive `server.token`.
- Browser dashboard auth uses an HttpOnly cookie named `miloco_dashboard_session`.
- Browser write requests use `X-Miloco-CSRF`.
- CLI/OpenClaw/Hermes/scripts keep using `Authorization: Bearer <server.token>`.
- All dashboard users are `admin` in this release.
- Minimum password length is 8 characters.
- Passwords are hashed with Argon2id through `argon2-cffi`.
- The database schema version moves from `2` to `3`.
- No password, password hash, session token, CSRF token, service token, Xiaomi token, model API key, RTSP URL, or camera frame may be printed or stored in logs, docs, memory, screenshots, or git history.
- Production deployment to `miloco.esxi` requires approved CO/PAM and a pre-deploy data backup.

---

## File Structure

### Backend auth package

- Create `backend/miloco/src/miloco/auth/__init__.py`
  - Exports only safe public auth types and service helpers.
- Create `backend/miloco/src/miloco/auth/schema.py`
  - Pydantic request/response models for setup, login, users, and sessions.
- Create `backend/miloco/src/miloco/auth/passwords.py`
  - Password normalization, policy validation, Argon2id hashing, hash verification.
- Create `backend/miloco/src/miloco/auth/repo.py`
  - SQLite persistence for `dashboard_user` and `dashboard_session`.
- Create `backend/miloco/src/miloco/auth/service.py`
  - First-admin setup, login, logout, status, user admin, session invalidation.
- Create `backend/miloco/src/miloco/auth/dependencies.py`
  - FastAPI request/WebSocket dependencies for service-token or dashboard-session auth and CSRF checks.
- Create `backend/miloco/src/miloco/auth/router.py`
  - `/api/auth/*` and `/api/users/*` routes.

### Existing backend files

- Modify `backend/miloco/pyproject.toml`
  - Add `argon2-cffi`.
- Modify `backend/uv.lock`
  - Refresh backend dependency lock.
- Modify `backend/miloco/src/miloco/database/connector.py`
  - Add schema version 3, table creation, and v2→v3 migration.
- Modify `backend/miloco/src/miloco/middleware/auth_middleware.py`
  - Preserve service-token helpers and route legacy imports to the new auth dependency where safe.
- Modify `backend/miloco/src/miloco/middleware/__init__.py`
  - Export new auth helpers.
- Modify `backend/miloco/src/miloco/main.py`
  - Include auth router, add CSRF middleware, stop service-token injection in `spa_handler`.
- Modify routers that currently import/use `verify_token_query_fallback` or `verify_websocket_token`
  - Update media/SSE/WebSocket paths to accept dashboard session cookies.

### CLI and installer

- Create `cli/src/miloco_cli/commands/auth.py`
  - `miloco-cli auth status`, `setup`, and `reset-password`.
- Modify `cli/src/miloco_cli/main.py`
  - Register `auth_group`.
- Modify `cli/src/miloco_cli/client.py`
  - Keep service-token behavior unchanged; add timeout override for auth setup/reset if needed.
- Modify `scripts/install.py`
  - Add dashboard admin setup step after service startup.
- Modify `scripts/i18n/zh.json` and `scripts/i18n/en.json`
  - Add installer strings.

### Frontend

- Create `web/src/api/auth.ts`
  - Auth and user-management API functions.
- Modify `web/src/api/client.ts`
  - Use `credentials: "same-origin"`, CSRF token setter/getter, no dashboard `server.token` dependency.
- Modify `web/src/api/register.ts`
  - Stop building `Authorization` headers from `window.__MILOCO_TOKEN__`; use cookie-mode fetch headers.
- Modify `web/src/api/real.ts`
  - Remove query `server.token` construction for clip/ref/SSE URLs; rely on cookie session or short-lived media token if backend tests prove cookie cannot cover a path.
- Create `web/src/components/AuthGate.tsx`
  - Startup state machine for setup/login/dashboard.
- Create `web/src/components/LoginPage.tsx`
  - Login form.
- Create `web/src/components/SetupAdminPage.tsx`
  - First administrator setup form.
- Create `web/src/components/DashboardUserMenu.tsx`
  - Current user and logout UI.
- Create `web/src/components/UsersPage.tsx`
  - User list, create/edit/password/delete UI.
- Create `web/src/lib/auth.ts`
  - Pure validation and mapping helpers for tests.
- Modify `web/src/App.tsx`
  - Wrap existing dashboard shell with `AuthGate`, render Users tab.
- Modify `web/src/components/Sidebar.tsx`
  - Add `users` tab.
- Modify `web/src/i18n/locales/{zh,en}/nav.json`
  - Add Users tab labels.
- Create `web/src/i18n/locales/{zh,en}/auth.json`
  - Login/setup/user-management copy.
- Modify `web/src/i18n/index.ts`
  - Load the new auth namespace if explicit namespace registration is required.

### Tests

- Create `backend/miloco/tests/auth/test_passwords.py`
- Create `backend/miloco/tests/auth/test_repo.py`
- Create `backend/miloco/tests/auth/test_auth_router.py`
- Create `backend/miloco/tests/auth/test_auth_dependencies.py`
- Create `backend/miloco/tests/auth/test_spa_auth_boundary.py`
- Create `backend/miloco/tests/auth/test_media_auth.py`
- Create `cli/tests/test_auth_commands.py`
- Create `web/tests/auth-client.test.ts`
- Create `web/tests/auth-flow.test.ts`
- Create `web/tests/users-page.test.ts`
- Update existing tests that assert token injection behavior, especially `web/tests/live-player-url.test.ts`.

---

### Task 1: Backend schema, password hashing, and auth repository

**Files:**

- Create: `backend/miloco/src/miloco/auth/__init__.py`
- Create: `backend/miloco/src/miloco/auth/schema.py`
- Create: `backend/miloco/src/miloco/auth/passwords.py`
- Create: `backend/miloco/src/miloco/auth/repo.py`
- Modify: `backend/miloco/pyproject.toml`
- Modify: `backend/uv.lock`
- Modify: `backend/miloco/src/miloco/database/connector.py`
- Test: `backend/miloco/tests/auth/test_passwords.py`
- Test: `backend/miloco/tests/auth/test_repo.py`
- Test: `backend/miloco/tests/auth/test_schema_migration.py`

**Interfaces:**

- Produces `DashboardUserRecord`, `DashboardSessionRecord`, and `DashboardUserPublic` in `miloco.auth.schema`.
- Produces `validate_password_policy(password: str) -> None`, `hash_password(password: str) -> str`, and `verify_password(password: str, password_hash: str) -> bool` in `miloco.auth.passwords`.
- Produces `DashboardAuthRepo` with:
  - `any_user_exists() -> bool`
  - `any_enabled_user() -> bool`
  - `count_enabled_admins(exclude_user_id: str | None = None) -> int`
  - `create_user(username: str, display_name: str, password_hash: str, role: str = "admin", enabled: bool = True) -> DashboardUserRecord`
  - `get_user_by_id(user_id: str) -> DashboardUserRecord | None`
  - `get_user_by_username(username: str) -> DashboardUserRecord | None`
  - `list_users() -> list[DashboardUserRecord]`
  - `update_user(user_id: str, *, username: str | None = None, display_name: str | None = None, enabled: bool | None = None) -> DashboardUserRecord`
  - `update_password(user_id: str, password_hash: str) -> DashboardUserRecord`
  - `delete_user(user_id: str) -> bool`
  - `create_session(user_id: str, session_hash: str, csrf_hash: str, expires_at: int, user_agent_hash: str, client_ip_hint: str | None) -> DashboardSessionRecord`
  - `get_session_by_hash(session_hash: str, now_ms: int) -> DashboardSessionRecord | None`
  - `delete_session(session_id: str) -> bool`
  - `delete_sessions_for_user(user_id: str) -> int`
  - `delete_expired_sessions(now_ms: int) -> int`
- Later tasks consume these interfaces exactly.

- [ ] **Step 1: Add failing password hashing tests**

Add `backend/miloco/tests/auth/test_passwords.py`:

```python
import pytest

from miloco.auth.passwords import (
    PasswordPolicyError,
    hash_password,
    validate_password_policy,
    verify_password,
)


def test_hash_password_uses_argon2_and_never_echoes_plaintext() -> None:
    password_hash = hash_password("correct horse battery")
    assert password_hash.startswith("$argon2")
    assert "correct horse battery" not in password_hash
    assert verify_password("correct horse battery", password_hash) is True
    assert verify_password("wrong horse battery", password_hash) is False


def test_password_policy_requires_eight_characters() -> None:
    with pytest.raises(PasswordPolicyError):
        validate_password_policy("short")
    validate_password_policy("12345678")
```

Run: `cd backend && uv run pytest miloco/tests/auth/test_passwords.py -v`

Expected: FAIL because `miloco.auth.passwords` does not exist.

- [ ] **Step 2: Add failing repository/schema tests**

Add `backend/miloco/tests/auth/test_repo.py`:

```python
from miloco.auth.repo import DashboardAuthRepo
from miloco.auth.passwords import hash_password
from miloco.config import reset_settings
from miloco.database.connector import init_database


def test_user_crud_never_returns_password_hash_in_public_model(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(tmp_path / "miloco.db"))
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
```

Add `backend/miloco/tests/auth/test_schema_migration.py`:

```python
import sqlite3

from miloco.config import reset_settings
from miloco.database.connector import init_database


def test_fresh_database_creates_dashboard_auth_tables(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "miloco.db"
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_path))
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
```

Run: `cd backend && uv run pytest miloco/tests/auth/test_repo.py miloco/tests/auth/test_schema_migration.py -v`

Expected: FAIL because tables and repo do not exist.

- [ ] **Step 3: Add dependency and refresh lock**

Modify `backend/miloco/pyproject.toml` dependencies:

```toml
  "argon2-cffi>=23.1.0",
```

Run: `cd backend && uv lock`

Expected: `backend/uv.lock` records `argon2-cffi`.

- [ ] **Step 4: Implement schema models**

Create `backend/miloco/src/miloco/auth/schema.py`:

```python
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class DashboardUserPublic(BaseModel):
    id: str
    username: str
    display_name: str = ""
    role: Literal["admin"] = "admin"
    enabled: bool
    created_at: int
    updated_at: int
    last_login_at: int | None = None


class DashboardUserRecord(DashboardUserPublic):
    username_norm: str
    password_hash: str

    def to_public(self) -> DashboardUserPublic:
        return DashboardUserPublic(
            id=self.id,
            username=self.username,
            display_name=self.display_name,
            role=self.role,
            enabled=self.enabled,
            created_at=self.created_at,
            updated_at=self.updated_at,
            last_login_at=self.last_login_at,
        )


class DashboardSessionRecord(BaseModel):
    id: str
    user_id: str
    session_hash: str
    csrf_hash: str
    created_at: int
    expires_at: int
    last_seen_at: int
    user_agent_hash: str
    client_ip_hint: str | None = None


class SetupRequest(BaseModel):
    username: str = Field(min_length=1)
    display_name: str = ""
    password: str = Field(min_length=1)
    password_confirm: str = Field(min_length=1)

    @field_validator("username")
    @classmethod
    def _clean_username(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("username_required")
        return cleaned


class LoginRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class UserCreateRequest(SetupRequest):
    pass


class UserUpdateRequest(BaseModel):
    username: str | None = None
    display_name: str | None = None
    enabled: bool | None = None


class PasswordChangeRequest(BaseModel):
    password: str = Field(min_length=1)
    password_confirm: str = Field(min_length=1)


class AuthStatusData(BaseModel):
    needs_setup: bool
    authenticated: bool
    user: DashboardUserPublic | None = None
    csrf_token: str | None = None


class UserListData(BaseModel):
    users: list[DashboardUserPublic]
```

- [ ] **Step 5: Implement password helpers**

Create `backend/miloco/src/miloco/auth/passwords.py`:

```python
from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError


class PasswordPolicyError(ValueError):
    pass


_PASSWORD_HASHER = PasswordHasher()


def validate_password_policy(password: str) -> None:
    if len(password) < 8:
        raise PasswordPolicyError("password_too_short")


def hash_password(password: str) -> str:
    validate_password_policy(password)
    return _PASSWORD_HASHER.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except (VerificationError, VerifyMismatchError):
        return False
```

- [ ] **Step 6: Implement schema version 3 tables**

Modify `backend/miloco/src/miloco/database/connector.py`:

```python
_DB_SCHEMA_VERSION = 3
```

Add fresh-create calls inside `_create_tables()` after `_create_kv_table(conn)`:

```python
self._create_dashboard_user_table(conn)
self._create_dashboard_session_table(conn)
```

Add missing-table recovery in `initialize_database()`:

```python
if "dashboard_user" not in existing_tables:
    logger.info("dashboard_user table not found, creating...")
    self._create_dashboard_user_table(conn)
    tables_created.append("dashboard_user")

if "dashboard_session" not in existing_tables:
    logger.info("dashboard_session table not found, creating...")
    self._create_dashboard_session_table(conn)
    tables_created.append("dashboard_session")
```

Add module-level table helpers plus thin instance wrappers. Use the helpers from both fresh database creation and migrations so the two paths cannot drift:

```python
def _create_dashboard_user_table_on(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dashboard_user (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            username_norm TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL DEFAULT '',
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'admin',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            last_login_at INTEGER
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dashboard_user_enabled "
        "ON dashboard_user(enabled)"
    )


def _create_dashboard_session_table_on(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dashboard_session (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            session_hash TEXT NOT NULL UNIQUE,
            csrf_hash TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            last_seen_at INTEGER NOT NULL,
            user_agent_hash TEXT NOT NULL,
            client_ip_hint TEXT,
            FOREIGN KEY(user_id) REFERENCES dashboard_user(id) ON DELETE CASCADE
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dashboard_session_user "
        "ON dashboard_session(user_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dashboard_session_expires "
        "ON dashboard_session(expires_at)"
    )


def _create_dashboard_user_table(self, conn: sqlite3.Connection) -> None:
    _create_dashboard_user_table_on(conn)


def _create_dashboard_session_table(self, conn: sqlite3.Connection) -> None:
    _create_dashboard_session_table_on(conn)
```

Add migration:

```python
def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute("BEGIN IMMEDIATE")
    try:
        _create_dashboard_user_table_on(conn)
        _create_dashboard_session_table_on(conn)
        cursor.execute("PRAGMA user_version = 3")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


_SCHEMA_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _migrate_v1_to_v2,
    3: _migrate_v2_to_v3,
}
```

- [ ] **Step 7: Implement repository**

Create `backend/miloco/src/miloco/auth/repo.py`:

```python
from __future__ import annotations

import sqlite3
import uuid

from miloco.auth.schema import DashboardSessionRecord, DashboardUserRecord
from miloco.database.connector import get_db_connector
from miloco.utils.time_utils import now_ms


def normalize_username(username: str) -> str:
    return username.strip().casefold()


class DashboardAuthRepo:
    def __init__(self) -> None:
        self.db = get_db_connector()

    def _row_to_user(self, row: dict) -> DashboardUserRecord:
        return DashboardUserRecord(
            id=row["id"],
            username=row["username"],
            username_norm=row["username_norm"],
            display_name=row.get("display_name") or "",
            password_hash=row["password_hash"],
            role=row.get("role") or "admin",
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_login_at=row.get("last_login_at"),
        )

    def _row_to_session(self, row: dict) -> DashboardSessionRecord:
        return DashboardSessionRecord(**row)

    def any_user_exists(self) -> bool:
        rows = self.db.execute_query("SELECT COUNT(*) AS n FROM dashboard_user")
        return bool(rows and rows[0]["n"] > 0)

    def any_enabled_user(self) -> bool:
        rows = self.db.execute_query(
            "SELECT COUNT(*) AS n FROM dashboard_user WHERE enabled = 1"
        )
        return bool(rows and rows[0]["n"] > 0)

    def count_enabled_admins(self, exclude_user_id: str | None = None) -> int:
        if exclude_user_id:
            rows = self.db.execute_query(
                "SELECT COUNT(*) AS n FROM dashboard_user "
                "WHERE enabled = 1 AND role = 'admin' AND id != ?",
                (exclude_user_id,),
            )
        else:
            rows = self.db.execute_query(
                "SELECT COUNT(*) AS n FROM dashboard_user "
                "WHERE enabled = 1 AND role = 'admin'"
            )
        return int(rows[0]["n"]) if rows else 0

    def create_user(
        self,
        username: str,
        display_name: str,
        password_hash: str,
        role: str = "admin",
        enabled: bool = True,
    ) -> DashboardUserRecord:
        ts = now_ms()
        user_id = uuid.uuid4().hex
        try:
            self.db.execute_update(
                """
                INSERT INTO dashboard_user
                  (id, username, username_norm, display_name, password_hash,
                   role, enabled, created_at, updated_at, last_login_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    user_id,
                    username.strip(),
                    normalize_username(username),
                    display_name.strip(),
                    password_hash,
                    role,
                    1 if enabled else 0,
                    ts,
                    ts,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("username_exists") from exc
        user = self.get_user_by_id(user_id)
        assert user is not None
        return user
```

Continue the file with the remaining interface methods named above. Use parameterized SQL only. Do not log usernames together with password-hash values.

- [ ] **Step 8: Run focused backend foundation tests**

Run:

```bash
cd backend
uv run pytest \
  miloco/tests/auth/test_passwords.py \
  miloco/tests/auth/test_repo.py \
  miloco/tests/auth/test_schema_migration.py \
  -v
```

Expected: PASS.

- [ ] **Step 9: Commit Task 1**

```bash
git add backend/miloco/pyproject.toml backend/uv.lock \
  backend/miloco/src/miloco/auth \
  backend/miloco/src/miloco/database/connector.py \
  backend/miloco/tests/auth/test_passwords.py \
  backend/miloco/tests/auth/test_repo.py \
  backend/miloco/tests/auth/test_schema_migration.py
git commit -m "feat: add dashboard auth persistence"
```

---

### Task 2: Auth service and HTTP API

**Files:**

- Create: `backend/miloco/src/miloco/auth/service.py`
- Create: `backend/miloco/src/miloco/auth/router.py`
- Modify: `backend/miloco/src/miloco/main.py`
- Test: `backend/miloco/tests/auth/test_auth_router.py`

**Interfaces:**

- Consumes Task 1 `DashboardAuthRepo`, password helpers, and schema models.
- Produces:
  - `SESSION_COOKIE_NAME = "miloco_dashboard_session"`
  - `CSRF_HEADER_NAME = "X-Miloco-CSRF"`
  - `SESSION_TTL_MS = 7 * 24 * 60 * 60 * 1000`
  - `hash_secret(secret: str) -> str`
  - `AuthService.status(request: Request) -> AuthStatusData`
  - `AuthService.setup_first_admin(body: SetupRequest, request: Request, response: Response) -> AuthStatusData`
  - `AuthService.login(body: LoginRequest, request: Request, response: Response) -> AuthStatusData`
  - `AuthService.logout(request: Request, response: Response) -> None`
  - `AuthService.list_users() -> list[DashboardUserPublic]`
  - `AuthService.create_user(body: UserCreateRequest) -> DashboardUserPublic`
  - `AuthService.update_user(user_id: str, body: UserUpdateRequest, current_session_user_id: str | None) -> DashboardUserPublic`
  - `AuthService.change_password(user_id: str, body: PasswordChangeRequest) -> DashboardUserPublic`
  - `AuthService.delete_user(user_id: str, current_session_user_id: str | None) -> None`

- [ ] **Step 1: Add failing router tests for setup/login/logout**

Add `backend/miloco/tests/auth/test_auth_router.py`:

```python
from fastapi.testclient import TestClient

from miloco.config import reset_settings
from miloco.database.connector import init_database
from miloco.main import app


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(tmp_path / "miloco.db"))
    monkeypatch.setenv("MILOCO_SERVER__TOKEN", "service-token")
    reset_settings()
    init_database()
    return TestClient(app)


def test_auth_status_requires_setup_on_fresh_db(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    response = client.get("/api/auth/status")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["needs_setup"] is True
    assert data["authenticated"] is False
    assert data["user"] is None
    assert data["csrf_token"] is None


def test_setup_creates_first_admin_and_sets_cookie(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
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
    assert "miloco_dashboard_session=" in response.headers["set-cookie"]
    data = response.json()["data"]
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
    client.post(
        "/api/auth/setup",
        json={
            "username": "lynx",
            "display_name": "Lynx",
            "password": "correct horse battery",
            "password_confirm": "correct horse battery",
        },
    )
    response = TestClient(app).post(
        "/api/auth/login",
        json={"username": "lynx", "password": "wrong password"},
    )
    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    body = response.json()
    assert body["code"] == 1003
    assert "password_hash" not in response.text


def test_logout_clears_session_cookie(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    client.post(
        "/api/auth/setup",
        json={
            "username": "lynx",
            "display_name": "Lynx",
            "password": "correct horse battery",
            "password_confirm": "correct horse battery",
        },
    )
    response = client.post("/api/auth/logout")
    assert response.status_code == 200
    assert "miloco_dashboard_session=" in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]
```

Run: `cd backend && uv run pytest miloco/tests/auth/test_auth_router.py -v`

Expected: FAIL because router/service do not exist.

- [ ] **Step 2: Implement service token hashing and cookie helpers**

In `backend/miloco/src/miloco/auth/service.py`:

```python
from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import Request, Response

from miloco.auth.passwords import PasswordPolicyError, hash_password, verify_password
from miloco.auth.repo import DashboardAuthRepo
from miloco.auth.schema import (
    AuthStatusData,
    DashboardUserPublic,
    LoginRequest,
    PasswordChangeRequest,
    SetupRequest,
    UserCreateRequest,
    UserUpdateRequest,
)
from miloco.middleware.exceptions import (
    AuthenticationException,
    BadRequestException,
    ConflictException,
    ResourceNotFoundException,
)
from miloco.utils.time_utils import now_ms

SESSION_COOKIE_NAME = "miloco_dashboard_session"
CSRF_HEADER_NAME = "X-Miloco-CSRF"
SESSION_TTL_MS = 7 * 24 * 60 * 60 * 1000


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _client_ip_hint(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host[:64]


def _user_agent_hash(request: Request) -> str:
    return hash_secret(request.headers.get("user-agent", "")[:512])


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_MS // 1000,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )
```

- [ ] **Step 3: Implement `AuthService` setup/login/logout**

Continue `service.py`:

```python
class AuthService:
    def __init__(self, repo: DashboardAuthRepo | None = None) -> None:
        self.repo = repo or DashboardAuthRepo()

    def _issue_session(
        self,
        user: DashboardUserPublic,
        request: Request,
        response: Response,
    ) -> str:
        session_token = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(32)
        now = now_ms()
        self.repo.create_session(
            user_id=user.id,
            session_hash=hash_secret(session_token),
            csrf_hash=hash_secret(csrf_token),
            expires_at=now + SESSION_TTL_MS,
            user_agent_hash=_user_agent_hash(request),
            client_ip_hint=_client_ip_hint(request),
        )
        self.repo.touch_user_login(user.id, now)
        _set_session_cookie(response, session_token)
        return csrf_token

    def status(self, request: Request) -> AuthStatusData:
        rotated = self.rotate_csrf(request)
        return AuthStatusData(
            needs_setup=not self.repo.any_enabled_user(),
            authenticated=rotated is not None,
            user=rotated[0] if rotated else None,
            csrf_token=rotated[1] if rotated else None,
        )

    def setup_first_admin(
        self,
        body: SetupRequest,
        request: Request,
        response: Response,
    ) -> AuthStatusData:
        if self.repo.any_user_exists():
            raise ConflictException("Dashboard setup already completed")
        if body.password != body.password_confirm:
            raise BadRequestException("Passwords do not match")
        try:
            password_hash = hash_password(body.password)
        except PasswordPolicyError as exc:
            raise BadRequestException(str(exc)) from exc
        user_record = self.repo.create_user(
            username=body.username,
            display_name=body.display_name,
            password_hash=password_hash,
        )
        csrf_token = self._issue_session(user_record.to_public(), request, response)
        return AuthStatusData(
            needs_setup=False,
            authenticated=True,
            user=user_record.to_public(),
            csrf_token=csrf_token,
        )

    def login(
        self,
        body: LoginRequest,
        request: Request,
        response: Response,
    ) -> AuthStatusData:
        user = self.repo.get_user_by_username(body.username)
        if user is None or not user.enabled or not verify_password(body.password, user.password_hash):
            raise AuthenticationException("Invalid username or password")
        csrf_token = self._issue_session(user.to_public(), request, response)
        return AuthStatusData(
            needs_setup=False,
            authenticated=True,
            user=user.to_public(),
            csrf_token=csrf_token,
        )

    def logout(self, request: Request, response: Response) -> None:
        token = request.cookies.get(SESSION_COOKIE_NAME)
        if token:
            session = self.repo.get_session_by_hash(hash_secret(token), now_ms())
            if session:
                self.repo.delete_session(session.id)
        _clear_session_cookie(response)
```

Add deterministic session-auth helpers. The service never stores CSRF plaintext; `/auth/status` and `/auth/me` rotate a fresh CSRF token for any valid cookie session.

```python
def authenticate_session_token_hash(
    self,
    session_hash: str,
) -> tuple[DashboardUserPublic, DashboardSessionRecord] | None:
    session = self.repo.get_session_by_hash(session_hash, now_ms())
    if session is None:
        return None
    user = self.repo.get_user_by_id(session.user_id)
    if user is None or not user.enabled:
        return None
    self.repo.touch_session(session.id, now_ms())
    return user.to_public(), session


def authenticate_request_session(
    self,
    request: Request,
) -> tuple[DashboardUserPublic, DashboardSessionRecord] | None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    return self.authenticate_session_token_hash(hash_secret(token))


def rotate_csrf(self, request: Request) -> tuple[DashboardUserPublic, str] | None:
    auth = self.authenticate_request_session(request)
    if auth is None:
        return None
    user, session = auth
    csrf_token = secrets.token_urlsafe(32)
    self.repo.update_session_csrf(session.id, hash_secret(csrf_token), now_ms())
    return user, csrf_token
```

- [ ] **Step 4: Implement user admin methods**

In `AuthService`, implement:

```python
def list_users(self) -> list[DashboardUserPublic]:
    return [u.to_public() for u in self.repo.list_users()]


def create_user(self, body: UserCreateRequest) -> DashboardUserPublic:
    if body.password != body.password_confirm:
        raise BadRequestException("Passwords do not match")
    try:
        user = self.repo.create_user(
            username=body.username,
            display_name=body.display_name,
            password_hash=hash_password(body.password),
        )
    except ValueError as exc:
        if str(exc) == "username_exists":
            raise ConflictException("Username already exists") from exc
        raise
    return user.to_public()
```

For update/delete:

```python
if body.enabled is False and self.repo.count_enabled_admins(exclude_user_id=user_id) == 0:
    raise ConflictException("Last administrator cannot be disabled")

if current_session_user_id == user_id:
    raise ConflictException("Current user cannot be deleted")

if self.repo.count_enabled_admins(exclude_user_id=user_id) == 0:
    raise ConflictException("Last administrator cannot be deleted")
```

Deleting or disabling a user calls `repo.delete_sessions_for_user(user_id)`.

- [ ] **Step 5: Implement auth and users router**

Create `backend/miloco/src/miloco/auth/router.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from miloco.auth.schema import (
    LoginRequest,
    PasswordChangeRequest,
    SetupRequest,
    UserCreateRequest,
    UserUpdateRequest,
)
from miloco.auth.service import AuthService
from miloco.schema.common_schema import NormalResponse

router = APIRouter(tags=["Dashboard Auth"])


def get_auth_service() -> AuthService:
    return AuthService()


@router.get("/auth/status", response_model=NormalResponse)
def auth_status(request: Request, service: AuthService = Depends(get_auth_service)):
    return NormalResponse(code=0, message="ok", data=service.status(request).model_dump())


@router.post("/auth/setup", response_model=NormalResponse)
def auth_setup(
    body: SetupRequest,
    request: Request,
    response: Response,
    service: AuthService = Depends(get_auth_service),
):
    data = service.setup_first_admin(body, request, response)
    return NormalResponse(code=0, message="ok", data=data.model_dump())
```

Continue with `/auth/login`, `/auth/logout`, `/auth/me`, `/users`, `/users/{user_id}`, `/users/{user_id}/password`, and delete. Import the protected dependency from Task 3 once it exists; during Task 2 use `Depends(verify_token)` for `/users/*` so service-token tests can be written, then Task 3 swaps it to dashboard-or-service.

- [ ] **Step 6: Register router in `main.py`**

Add:

```python
from miloco.auth.router import router as auth_router
```

Before other API routers:

```python
app.include_router(auth_router, prefix="/api")
```

- [ ] **Step 7: Run focused auth router tests**

Run:

```bash
cd backend
uv run pytest miloco/tests/auth/test_auth_router.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit Task 2**

```bash
git add backend/miloco/src/miloco/auth/service.py \
  backend/miloco/src/miloco/auth/router.py \
  backend/miloco/src/miloco/main.py \
  backend/miloco/tests/auth/test_auth_router.py
git commit -m "feat: add dashboard auth api"
```

---

### Task 3: Combined route authentication and CSRF guard

**Files:**

- Create: `backend/miloco/src/miloco/auth/dependencies.py`
- Modify: `backend/miloco/src/miloco/middleware/auth_middleware.py`
- Modify: `backend/miloco/src/miloco/middleware/__init__.py`
- Modify: `backend/miloco/src/miloco/main.py`
- Modify: `backend/miloco/src/miloco/auth/router.py`
- Test: `backend/miloco/tests/auth/test_auth_dependencies.py`

**Interfaces:**

- Consumes Task 2 `AuthService`, `hash_secret`, `SESSION_COOKIE_NAME`, and `CSRF_HEADER_NAME`.
- Produces:
  - `AuthContext(BaseModel)` with `kind: Literal["service", "dashboard"]`, `subject: str`, `user: DashboardUserPublic | None`.
  - `verify_service_token(request: Request) -> AuthContext`
  - `verify_dashboard_or_service_auth(request: Request) -> AuthContext`
  - `verify_dashboard_or_service_query_fallback(request: Request) -> AuthContext`
  - `verify_websocket_dashboard_or_service(websocket: WebSocket) -> AuthContext`
  - `require_csrf_for_cookie_writes(request: Request) -> None`
- Keeps legacy imports working:
  - `verify_token = verify_dashboard_or_service_auth`
  - `verify_token_query_fallback = verify_dashboard_or_service_query_fallback`
  - `verify_websocket_token = verify_websocket_dashboard_or_service`

- [ ] **Step 1: Add failing auth dependency tests**

Add `backend/miloco/tests/auth/test_auth_dependencies.py`:

```python
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from miloco.auth.router import router as auth_router
from miloco.auth.service import CSRF_HEADER_NAME
from miloco.config import reset_settings
from miloco.database.connector import init_database
from miloco.middleware import verify_token
from miloco.middleware.exception_handler import handle_exception
from miloco.schema.common_schema import NormalResponse


def _app() -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _catch(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:
            return handle_exception(request, exc)

    app.include_router(auth_router, prefix="/api")

    @app.get("/api/protected")
    def protected(_auth=Depends(verify_token)):
        return NormalResponse(code=0, message="ok", data={"ok": True})

    @app.post("/api/protected-write")
    def protected_write(_auth=Depends(verify_token)):
        return NormalResponse(code=0, message="ok", data={"ok": True})

    return app


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(tmp_path / "miloco.db"))
    monkeypatch.setenv("MILOCO_SERVER__TOKEN", "service-token")
    reset_settings()
    init_database()
    return TestClient(_app())


def test_service_token_still_accesses_protected_routes(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    response = client.get("/api/protected", headers={"Authorization": "Bearer service-token"})
    assert response.status_code == 200


def test_dashboard_session_accesses_protected_routes(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    setup = client.post(
        "/api/auth/setup",
        json={
            "username": "lynx",
            "display_name": "Lynx",
            "password": "correct horse battery",
            "password_confirm": "correct horse battery",
        },
    )
    csrf = setup.json()["data"]["csrf_token"]
    assert client.get("/api/protected").status_code == 200
    assert client.post("/api/protected-write").status_code == 403
    assert client.post("/api/protected-write", headers={CSRF_HEADER_NAME: csrf}).status_code == 200


def test_unauthenticated_request_is_401(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    assert client.get("/api/protected").status_code == 401
```

Run: `cd backend && uv run pytest miloco/tests/auth/test_auth_dependencies.py -v`

Expected: FAIL because combined dependency and CSRF behavior do not exist.

- [ ] **Step 2: Implement service-token check as a separate helper**

In `backend/miloco/src/miloco/auth/dependencies.py`:

```python
from __future__ import annotations

from typing import Literal

from fastapi import Request
from fastapi.websockets import WebSocket
from pydantic import BaseModel

from miloco.auth.schema import DashboardUserPublic
from miloco.auth.service import (
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    AuthService,
    hash_secret,
)
from miloco.config import get_settings
from miloco.middleware.exceptions import AuthenticationException, AuthorizationException

BEARER_PREFIX = "Bearer "
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
PUBLIC_AUTH_PATHS = {
    "/api/auth/status",
    "/api/auth/setup",
    "/api/auth/login",
    "/api/auth/logout",
}


class AuthContext(BaseModel):
    kind: Literal["service", "dashboard"]
    subject: str
    user: DashboardUserPublic | None = None


def extract_bearer_token(authorization: str | None) -> str | None:
    if authorization and authorization.startswith(BEARER_PREFIX):
        return authorization[len(BEARER_PREFIX):]
    return None


def valid_service_token(token: str | None) -> bool:
    expected = get_settings().server.token
    if not expected:
        return False
    return token is not None and hmac.compare_digest(token, expected)
```

Remember to import `hmac`.

- [ ] **Step 3: Implement dashboard/session dependency**

Continue `dependencies.py`:

```python
def verify_service_token(request: Request) -> AuthContext:
    if valid_service_token(extract_bearer_token(request.headers.get("Authorization"))):
        return AuthContext(kind="service", subject="service")
    raise AuthenticationException("Invalid or missing service token")


def _dashboard_context(request: Request) -> AuthContext | None:
    auth = AuthService().authenticate_request_session(request)
    if auth is None:
        return None
    user, _session = auth
    return AuthContext(kind="dashboard", subject=user.id, user=user)


def verify_dashboard_or_service_auth(request: Request) -> AuthContext:
    token = extract_bearer_token(request.headers.get("Authorization"))
    if valid_service_token(token):
        return AuthContext(kind="service", subject="service")
    dashboard = _dashboard_context(request)
    if dashboard:
        return dashboard
    raise AuthenticationException("Authentication required")
```

- [ ] **Step 4: Implement query fallback and WebSocket auth**

```python
def verify_dashboard_or_service_query_fallback(request: Request) -> AuthContext:
    token = (
        extract_bearer_token(request.headers.get("Authorization"))
        or request.query_params.get("token")
    )
    if valid_service_token(token):
        return AuthContext(kind="service", subject="service")
    dashboard = _dashboard_context(request)
    if dashboard:
        return dashboard
    raise AuthenticationException("Authentication required")


def verify_websocket_dashboard_or_service(websocket: WebSocket) -> AuthContext:
    token = (
        extract_bearer_token(websocket.headers.get("Authorization"))
        or websocket.query_params.get("token")
    )
    if valid_service_token(token):
        return AuthContext(kind="service", subject="service")
    cookie_token = websocket.cookies.get(SESSION_COOKIE_NAME)
    if cookie_token:
        auth = AuthService().authenticate_session_token_hash(hash_secret(cookie_token))
        if auth is not None:
            user, _session = auth
            return AuthContext(kind="dashboard", subject=user.id, user=user)
    raise AuthenticationException("Authentication required")
```

- [ ] **Step 5: Implement CSRF guard**

Add to `dependencies.py`:

```python
def require_csrf_for_cookie_writes(request: Request) -> None:
    if request.method.upper() not in UNSAFE_METHODS:
        return
    if request.url.path in PUBLIC_AUTH_PATHS:
        return
    if valid_service_token(extract_bearer_token(request.headers.get("Authorization"))):
        return
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie_token:
        return
    session = AuthService().session_for_token_hash(hash_secret(cookie_token))
    if session is None:
        return
    csrf = request.headers.get(CSRF_HEADER_NAME)
    if not csrf or not hmac.compare_digest(hash_secret(csrf), session.csrf_hash):
        raise AuthorizationException("CSRF token missing or invalid")
```

Add imports for `hmac` and `AuthorizationException`.

- [ ] **Step 6: Wire CSRF middleware in `main.py`**

Add:

```python
from miloco.auth.dependencies import require_csrf_for_cookie_writes
```

Inside the existing HTTP middleware wrapper, before `call_next(request)`:

```python
require_csrf_for_cookie_writes(request)
```

Do not add a second global middleware if `main.py` already has a catch-all exception middleware. Keep exception handling path unified through `handle_exception`.

- [ ] **Step 7: Preserve legacy imports**

Modify `backend/miloco/src/miloco/middleware/auth_middleware.py`:

```python
from miloco.auth.dependencies import (
    extract_bearer_token as _extract_bearer_token,
    verify_dashboard_or_service_auth as verify_token,
    verify_dashboard_or_service_query_fallback as verify_token_query_fallback,
    verify_service_token,
    verify_websocket_dashboard_or_service as verify_websocket_token,
)
```

Modify `backend/miloco/src/miloco/middleware/__init__.py` to export `verify_service_token` and keep existing names.

- [ ] **Step 8: Protect auth user-management routes with combined auth**

Modify `backend/miloco/src/miloco/auth/router.py`:

```python
from miloco.auth.dependencies import AuthContext, verify_dashboard_or_service_auth


@router.get("/auth/me", response_model=NormalResponse)
def auth_me(
    request: Request,
    _auth: AuthContext = Depends(verify_dashboard_or_service_auth),
    service: AuthService = Depends(get_auth_service),
):
    data = service.status(request)
    return NormalResponse(code=0, message="ok", data=data.model_dump())
```

Use the same dependency on `/users/*`. Pass `current_session_user_id=_auth.subject if _auth.kind == "dashboard" else None` to update/delete methods.

- [ ] **Step 9: Run dependency tests and an existing route smoke**

Run:

```bash
cd backend
uv run pytest \
  miloco/tests/auth/test_auth_dependencies.py \
  miloco/tests/auth/test_auth_router.py \
  miloco/tests/devices/test_router.py \
  -v
```

Expected: PASS. Existing service-token tests should still pass unchanged.

- [ ] **Step 10: Commit Task 3**

```bash
git add backend/miloco/src/miloco/auth/dependencies.py \
  backend/miloco/src/miloco/auth/router.py \
  backend/miloco/src/miloco/middleware/auth_middleware.py \
  backend/miloco/src/miloco/middleware/__init__.py \
  backend/miloco/src/miloco/main.py \
  backend/miloco/tests/auth/test_auth_dependencies.py
git commit -m "feat: protect dashboard api with sessions"
```

---

### Task 4: Remove dashboard service-token injection and fix browser media auth

**Files:**

- Modify: `backend/miloco/src/miloco/main.py`
- Modify: `backend/miloco/src/miloco/camera/router.py`
- Modify: `backend/miloco/src/miloco/miot/router.py`
- Modify: `backend/miloco/src/miloco/perception/events_router.py`
- Modify: `backend/miloco/src/miloco/perception/router.py`
- Modify: `backend/miloco/src/miloco/admin/router.py`
- Test: `backend/miloco/tests/auth/test_spa_auth_boundary.py`
- Test: `backend/miloco/tests/auth/test_media_auth.py`
- Update: `backend/miloco/tests/test_sse_auth.py`

**Interfaces:**

- Consumes Task 3 combined auth dependencies.
- Produces a dashboard HTML boundary where `/` and `/index.html` never contain a real `server.token`.
- Produces browser streaming behavior where same-origin cookie sessions can access watch pages, EventSource streams, WebSocket streams, clip URLs, and ref images.

- [ ] **Step 1: Add failing SPA boundary tests**

Add `backend/miloco/tests/auth/test_spa_auth_boundary.py`:

```python
from pathlib import Path

from fastapi.testclient import TestClient

from miloco.config import reset_settings
from miloco.database.connector import init_database
from miloco.main import app


def test_spa_root_does_not_inject_service_token(tmp_path, monkeypatch) -> None:
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(
        '<script>window.__MILOCO_TOKEN__ = "__MILOCO_INJECT_TOKEN_HERE__";</script>',
        encoding="utf-8",
    )
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(tmp_path / "miloco.db"))
    monkeypatch.setenv("MILOCO_SERVER__TOKEN", "service-token-secret")
    monkeypatch.setenv("MILOCO_DIRECTORIES__STATIC_DIR", str(static_dir))
    reset_settings()
    init_database()

    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert "service-token-secret" not in response.text
    assert "__MILOCO_INJECT_TOKEN_HERE__" not in response.text
```

If `MILOCO_DIRECTORIES__STATIC_DIR` is not a supported settings path, monkeypatch `get_settings().directories.static_dir` through the existing settings reset hook or create a temp `$MILOCO_HOME/storage/static/index.html` that matches the computed default.

Run: `cd backend && uv run pytest miloco/tests/auth/test_spa_auth_boundary.py -v`

Expected: FAIL because `spa_handler` still injects the token.

- [ ] **Step 2: Add failing media/session auth tests**

Add `backend/miloco/tests/auth/test_media_auth.py`:

```python
from fastapi import APIRouter, Depends, FastAPI, Request, WebSocket
from fastapi.testclient import TestClient

from miloco.auth.router import router as auth_router
from miloco.config import reset_settings
from miloco.database.connector import init_database
from miloco.middleware import verify_token_query_fallback, verify_websocket_token
from miloco.middleware.exception_handler import handle_exception


def _app() -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _catch(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:
            return handle_exception(request, exc)

    router = APIRouter()

    @router.get("/media", dependencies=[Depends(verify_token_query_fallback)])
    def media():
        return {"ok": True}

    @router.websocket("/ws")
    async def ws(websocket: WebSocket, _auth=Depends(verify_websocket_token)):
        await websocket.accept()
        await websocket.send_text("ok")
        await websocket.close()

    app.include_router(auth_router, prefix="/api")
    app.include_router(router, prefix="/api")
    return app


def _logged_in_client(tmp_path, monkeypatch) -> tuple[TestClient, str]:
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(tmp_path / "miloco.db"))
    monkeypatch.setenv("MILOCO_SERVER__TOKEN", "service-token")
    reset_settings()
    init_database()
    client = TestClient(_app())
    setup = client.post(
        "/api/auth/setup",
        json={
            "username": "lynx",
            "display_name": "Lynx",
            "password": "correct horse battery",
            "password_confirm": "correct horse battery",
        },
    )
    return client, setup.json()["data"]["csrf_token"]


def test_cookie_session_can_load_media_without_query_token(tmp_path, monkeypatch) -> None:
    client, _csrf = _logged_in_client(tmp_path, monkeypatch)
    response = client.get("/api/media")
    assert response.status_code == 200


def test_cookie_session_can_open_websocket_without_query_token(tmp_path, monkeypatch) -> None:
    client, _csrf = _logged_in_client(tmp_path, monkeypatch)
    with client.websocket_connect("/api/ws") as websocket:
        assert websocket.receive_text() == "ok"
```

Run: `cd backend && uv run pytest miloco/tests/auth/test_media_auth.py -v`

Expected: FAIL if media/WebSocket dependencies still require query or Bearer tokens only.

- [ ] **Step 3: Remove service-token injection in `spa_handler`**

Modify `backend/miloco/src/miloco/main.py`:

```python
template = index_file.read_text(encoding="utf-8")
template = template.replace("__MILOCO_INJECT_TOKEN_HERE__", "")
return HTMLResponse(template, headers={"Cache-Control": "no-store"})
```

Also update comments in `spa_handler` so they state the new trust model: HTML is public, dashboard data is protected by auth/session.

- [ ] **Step 4: Ensure watch pages do not inject the service token**

In `backend/miloco/src/miloco/miot/router.py`, change `/api/miot/watch` so it no longer replaces `__MILOCO_TOKEN__` with `server.token`. It should either remove the placeholder or replace it with an empty string:

```python
html = template.replace("__MILOCO_TOKEN__", "")
```

Keep `/api/miot/watch` protected by combined auth so a logged-in browser can load the page and same-origin cookies flow to its WebSocket requests.

- [ ] **Step 5: Update camera generic WebSocket auth**

In `backend/miloco/src/miloco/camera/router.py`, replace custom service-token-only `_verify_generic_websocket` logic with `verify_websocket_dashboard_or_service`.

Keep the `Sec-WebSocket-Protocol` handling for the existing `miloco-camera` subprotocol, but source the auth decision from Task 3.

- [ ] **Step 6: Update SSE and media endpoints**

Update imports in:

- `backend/miloco/src/miloco/perception/events_router.py`
- `backend/miloco/src/miloco/perception/router.py`
- `backend/miloco/src/miloco/admin/router.py`

Keep function names `verify_token_query_fallback` through the legacy export, but confirm those names now accept dashboard session cookies. Add tests for `/api/events/stream`, event clip/ref URLs, on-demand clip URLs, and admin omni stream where feasible without blocking streaming generators.

- [ ] **Step 7: Run media/auth regression tests**

Run:

```bash
cd backend
uv run pytest \
  miloco/tests/auth/test_spa_auth_boundary.py \
  miloco/tests/auth/test_media_auth.py \
  miloco/tests/test_sse_auth.py \
  -v
```

Expected: PASS.

- [ ] **Step 8: Commit Task 4**

```bash
git add backend/miloco/src/miloco/main.py \
  backend/miloco/src/miloco/camera/router.py \
  backend/miloco/src/miloco/miot/router.py \
  backend/miloco/src/miloco/perception/events_router.py \
  backend/miloco/src/miloco/perception/router.py \
  backend/miloco/src/miloco/admin/router.py \
  backend/miloco/tests/auth/test_spa_auth_boundary.py \
  backend/miloco/tests/auth/test_media_auth.py \
  backend/miloco/tests/test_sse_auth.py
git commit -m "fix: stop exposing service token to dashboard"
```

---

### Task 5: Frontend auth gate, cookie-mode API client, login, setup, and logout

**Files:**

- Create: `web/src/api/auth.ts`
- Create: `web/src/lib/auth.ts`
- Create: `web/src/components/AuthGate.tsx`
- Create: `web/src/components/LoginPage.tsx`
- Create: `web/src/components/SetupAdminPage.tsx`
- Create: `web/src/components/DashboardUserMenu.tsx`
- Modify: `web/src/api/client.ts`
- Modify: `web/src/api/register.ts`
- Modify: `web/src/api/real.ts`
- Modify: `web/src/components/LivePlayerPlaceholder.tsx`
- Modify: `web/src/App.tsx`
- Create: `web/src/i18n/locales/zh/auth.json`
- Create: `web/src/i18n/locales/en/auth.json`
- Modify: `web/src/i18n/index.ts`
- Test: `web/tests/auth-client.test.ts`
- Test: `web/tests/auth-flow.test.ts`
- Update: `web/tests/live-player-url.test.ts`

**Interfaces:**

- Consumes backend routes from Tasks 2-4.
- Produces:
  - `setCsrfToken(token: string | null): void`
  - `getCsrfToken(): string`
  - `apiFetch<T>(path: string, init?: RequestInit): Promise<T>` with cookie credentials and automatic CSRF header on unsafe methods.
  - `getAuthStatus(): Promise<AuthStatus>`
  - `setupFirstAdmin(input: SetupAdminInput): Promise<AuthStatus>`
  - `login(input: LoginInput): Promise<AuthStatus>`
  - `logout(): Promise<void>`
  - `listDashboardUsers(): Promise<DashboardUser[]>`
  - `chooseAuthView(status: AuthStatus): "setup" | "login" | "dashboard"`

- [ ] **Step 1: Add failing client tests**

Add `web/tests/auth-client.test.ts`:

```ts
import { describe, expect, it, vi, beforeEach } from "vitest";
import { apiFetch, setCsrfToken } from "@/api/client";

describe("auth-aware apiFetch", () => {
  beforeEach(() => {
    setCsrfToken(null);
    vi.restoreAllMocks();
  });

  it("uses same-origin credentials and does not send bearer from window token", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ code: 0, data: {} }), { status: 200 })));
    (window as unknown as { __MILOCO_TOKEN__?: string }).__MILOCO_TOKEN__ = "service-token-secret";

    await apiFetch("/api/auth/status");

    const [_url, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(init.credentials).toBe("same-origin");
    expect(new Headers(init.headers).has("Authorization")).toBe(false);
  });

  it("adds csrf header to unsafe methods after login state is set", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ code: 0, data: {} }), { status: 200 })));
    setCsrfToken("csrf-token");

    await apiFetch("/api/rules", { method: "POST", body: "{}" });

    const [_url, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(new Headers(init.headers).get("X-Miloco-CSRF")).toBe("csrf-token");
  });
});
```

Run: `cd web && npm test -- auth-client.test.ts`

Expected: FAIL because `apiFetch` still uses injected Bearer token and lacks CSRF state.

- [ ] **Step 2: Add failing pure auth-flow tests**

Add `web/tests/auth-flow.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { chooseAuthView, validatePasswordPair } from "@/lib/auth";

describe("auth view selection", () => {
  it("shows setup before login when no user exists", () => {
    expect(chooseAuthView({ needsSetup: true, authenticated: false, user: null, csrfToken: null })).toBe("setup");
  });

  it("shows login when setup is complete but user is anonymous", () => {
    expect(chooseAuthView({ needsSetup: false, authenticated: false, user: null, csrfToken: null })).toBe("login");
  });

  it("shows dashboard only after authentication", () => {
    expect(chooseAuthView({
      needsSetup: false,
      authenticated: true,
      csrfToken: "csrf",
      user: {
        id: "u1",
        username: "lynx",
        displayName: "Lynx",
        role: "admin",
        enabled: true,
        createdAt: 1,
        updatedAt: 1,
        lastLoginAt: null,
      },
    })).toBe("dashboard");
  });
});

describe("password validation", () => {
  it("requires matching passwords with at least eight chars", () => {
    expect(validatePasswordPair("1234567", "1234567")).toBe("passwordTooShort");
    expect(validatePasswordPair("12345678", "87654321")).toBe("passwordMismatch");
    expect(validatePasswordPair("12345678", "12345678")).toBe(null);
  });
});
```

Run: `cd web && npm test -- auth-flow.test.ts`

Expected: FAIL because `web/src/lib/auth.ts` does not exist.

- [ ] **Step 3: Implement frontend auth types and pure helpers**

Create `web/src/lib/auth.ts`:

```ts
export interface DashboardUser {
  id: string;
  username: string;
  displayName: string;
  role: "admin";
  enabled: boolean;
  createdAt: number;
  updatedAt: number;
  lastLoginAt: number | null;
}

export interface AuthStatus {
  needsSetup: boolean;
  authenticated: boolean;
  user: DashboardUser | null;
  csrfToken: string | null;
}

export type AuthView = "setup" | "login" | "dashboard";

export function chooseAuthView(status: AuthStatus): AuthView {
  if (status.needsSetup) return "setup";
  if (!status.authenticated || !status.user) return "login";
  return "dashboard";
}

export type PasswordValidationError = "passwordTooShort" | "passwordMismatch";

export function validatePasswordPair(password: string, confirm: string): PasswordValidationError | null {
  if (password.length < 8) return "passwordTooShort";
  if (password !== confirm) return "passwordMismatch";
  return null;
}
```

- [ ] **Step 4: Change API client to cookie/CSRF mode**

Modify `web/src/api/client.ts`:

```ts
let csrfToken = "";

export function setCsrfToken(token: string | null): void {
  csrfToken = token ?? "";
}

export function getCsrfToken(): string {
  return csrfToken;
}

function isUnsafeMethod(method?: string): boolean {
  const normalized = (method ?? "GET").toUpperCase();
  return normalized === "POST" || normalized === "PUT" || normalized === "PATCH" || normalized === "DELETE";
}
```

In `apiFetch`:

```ts
const headers = new Headers(init?.headers);
if (csrfToken && isUnsafeMethod(init?.method)) {
  headers.set("X-Miloco-CSRF", csrfToken);
}
const resp = await fetch(path, { ...init, headers, credentials: "same-origin" });
```

Remove setting `Authorization` from `resolveToken()`. Keep `resolveToken()` temporarily returning `""` only until Step 6 removes every runtime import that still depends on it:

```ts
export function resolveToken(): string {
  return "";
}
```

After Step 6, remove the `resolveToken()` export entirely when this search has no runtime import hits:

```bash
rg -n "resolveToken\\(" web/src
```

- [ ] **Step 5: Add `web/src/api/auth.ts`**

```ts
import { apiFetch, setCsrfToken } from "./client";
import type { AuthStatus, DashboardUser } from "@/lib/auth";

interface Normal<T> { code: number; message: string; data: T; }

function mapUser(raw: Record<string, unknown>): DashboardUser {
  return {
    id: String(raw.id),
    username: String(raw.username),
    displayName: String(raw.display_name ?? ""),
    role: "admin",
    enabled: Boolean(raw.enabled),
    createdAt: Number(raw.created_at),
    updatedAt: Number(raw.updated_at),
    lastLoginAt: raw.last_login_at == null ? null : Number(raw.last_login_at),
  };
}

function mapStatus(raw: Record<string, unknown>): AuthStatus {
  const userRaw = raw.user as Record<string, unknown> | null | undefined;
  const status = {
    needsSetup: Boolean(raw.needs_setup),
    authenticated: Boolean(raw.authenticated),
    user: userRaw ? mapUser(userRaw) : null,
    csrfToken: typeof raw.csrf_token === "string" ? raw.csrf_token : null,
  };
  setCsrfToken(status.csrfToken);
  return status;
}

export async function getAuthStatus(): Promise<AuthStatus> {
  const resp = await apiFetch<Normal<Record<string, unknown>>>("/api/auth/status");
  return mapStatus(resp.data);
}

export async function setupFirstAdmin(input: {
  username: string;
  displayName: string;
  password: string;
  passwordConfirm: string;
}): Promise<AuthStatus> {
  const resp = await apiFetch<Normal<Record<string, unknown>>>("/api/auth/setup", {
    method: "POST",
    body: JSON.stringify({
      username: input.username,
      display_name: input.displayName,
      password: input.password,
      password_confirm: input.passwordConfirm,
    }),
  });
  return mapStatus(resp.data);
}
```

Continue with `login`, `logout`, `listDashboardUsers`, `createDashboardUser`, `updateDashboardUser`, `changeDashboardUserPassword`, and `deleteDashboardUser`. Each successful auth-status response must call `setCsrfToken`.

- [ ] **Step 6: Remove browser service-token URL building**

Modify `web/src/api/register.ts`:

```ts
export function authHeaders(extra?: Record<string, string>): Record<string, string> {
  return extra ?? {};
}
```

Modify `web/src/api/real.ts`:

```ts
export function realOnDemandClipUrl(logId: string, deviceId: string): string {
  return `/api/perception/on-demand-logs/${encodeURIComponent(logId)}/clip/${encodeURIComponent(deviceId)}`;
}

export function realEventClipUrl(event_id: string, device_id: string): string {
  return `/api/events/${encodeURIComponent(event_id)}/clip/${encodeURIComponent(device_id)}`;
}

export function realEventRefUrl(event_id: string, device_id: string): string {
  return `/api/events/${encodeURIComponent(event_id)}/ref/${encodeURIComponent(device_id)}`;
}

export function realSubscribeEvents(onEvent: (e: ActivityEvent) => void, onOpen?: () => void): () => void {
  const es = new EventSource("/api/events/stream");
  ...
}
```

Modify `web/src/components/LivePlayerPlaceholder.tsx` to remove the postMessage token exchange and keep `cameraWatchUrl` unchanged:

```ts
// Same-origin iframe and WebSocket requests authenticate with the dashboard session cookie.
```

- [ ] **Step 7: Implement auth gate and pages**

Create `AuthGate.tsx`:

```tsx
import { useEffect, useState } from "react";
import { getAuthStatus, login, logout, setupFirstAdmin } from "@/api/auth";
import { chooseAuthView, type AuthStatus } from "@/lib/auth";
import { LoginPage } from "./LoginPage";
import { SetupAdminPage } from "./SetupAdminPage";
import { toast } from "./Toast";

export function AuthGate({ children }: { children: (auth: AuthStatus, onLogout: () => Promise<void>) => React.ReactNode }) {
  const [status, setStatus] = useState<AuthStatus | null>(null);

  useEffect(() => {
    getAuthStatus().then(setStatus).catch(() => {
      toast("无法读取登录状态", "danger");
      setStatus({ needsSetup: false, authenticated: false, user: null, csrfToken: null });
    });
  }, []);

  if (!status) return <div className="h-screen grid place-items-center text-text-tertiary">正在检查登录状态…</div>;

  const view = chooseAuthView(status);
  if (view === "setup") return <SetupAdminPage onDone={setStatus} />;
  if (view === "login") return <LoginPage onDone={setStatus} />;

  return <>{children(status, async () => {
    await logout();
    setStatus(await getAuthStatus());
  })}</>;
}
```

Implement `LoginPage.tsx` and `SetupAdminPage.tsx` as controlled forms with password inputs and no local/session storage for password values.

- [ ] **Step 8: Add dashboard user menu**

Create `DashboardUserMenu.tsx`:

```tsx
import type { AuthStatus } from "@/lib/auth";

export function DashboardUserMenu({ auth, onLogout }: { auth: AuthStatus; onLogout: () => void | Promise<void> }) {
  const label = auth.user?.displayName || auth.user?.username || "用户";
  return (
    <div className="flex items-center gap-2 text-caption text-text-secondary">
      <span className="truncate max-w-[160px]">{label}</span>
      <button type="button" onClick={onLogout} className="px-2 py-1 rounded-md border border-border hover:border-border-strong">
        登出
      </button>
    </div>
  );
}
```

In `App.tsx`, split the current exported `App` into:

```tsx
export function App() {
  return (
    <AuthGate>
      {(auth, onLogout) => <DashboardApp auth={auth} onLogout={onLogout} />}
    </AuthGate>
  );
}
```

Move existing app body into `DashboardApp`. Render `DashboardUserMenu` in the top bar near language/theme controls.

- [ ] **Step 9: Add i18n strings**

Create `web/src/i18n/locales/zh/auth.json`:

```json
{
  "auth": {
    "checking": "正在检查登录状态…",
    "setupTitle": "创建第一个管理员",
    "setupSubtitle": "以后打开 Miloco dashboard 都需要使用这个账号登录。",
    "loginTitle": "登录 Miloco",
    "loginSubtitle": "请输入 dashboard 用户名和密码。",
    "username": "用户名",
    "displayName": "显示名",
    "password": "密码",
    "passwordConfirm": "确认密码",
    "login": "登录",
    "logout": "登出",
    "createAdmin": "创建管理员",
    "passwordTooShort": "密码至少需要 8 个字符",
    "passwordMismatch": "两次输入的密码不一致",
    "invalidLogin": "用户名或密码不正确",
    "statusFailed": "无法读取登录状态"
  }
}
```

Create English equivalent with matching keys.

- [ ] **Step 10: Run focused frontend tests**

Run:

```bash
cd web
npm test -- auth-client.test.ts auth-flow.test.ts live-player-url.test.ts
```

Expected: PASS.

- [ ] **Step 11: Commit Task 5**

```bash
git add web/src/api/auth.ts web/src/api/client.ts web/src/api/register.ts \
  web/src/api/real.ts web/src/lib/auth.ts \
  web/src/components/AuthGate.tsx web/src/components/LoginPage.tsx \
  web/src/components/SetupAdminPage.tsx web/src/components/DashboardUserMenu.tsx \
  web/src/components/LivePlayerPlaceholder.tsx web/src/App.tsx \
  web/src/i18n web/tests/auth-client.test.ts web/tests/auth-flow.test.ts \
  web/tests/live-player-url.test.ts
git commit -m "feat: add dashboard login flow"
```

---

### Task 6: Users tab and user-management frontend

**Files:**

- Create: `web/src/components/UsersPage.tsx`
- Modify: `web/src/api/auth.ts`
- Modify: `web/src/lib/auth.ts`
- Modify: `web/src/components/Sidebar.tsx`
- Modify: `web/src/App.tsx`
- Modify: `web/src/i18n/locales/zh/nav.json`
- Modify: `web/src/i18n/locales/en/nav.json`
- Modify: `web/src/i18n/locales/zh/auth.json`
- Modify: `web/src/i18n/locales/en/auth.json`
- Test: `web/tests/users-page.test.ts`

**Interfaces:**

- Consumes Task 5 auth API functions and `DashboardUser`.
- Produces:
  - `TabKey` includes `"users"`.
  - `canDeleteUser(user: DashboardUser, currentUserId: string | null, users: DashboardUser[]): boolean`
  - `canDisableUser(user: DashboardUser, users: DashboardUser[]): boolean`
  - `UsersPage` component with list/create/edit/password/delete flows.

- [ ] **Step 1: Add failing users-page pure tests**

Add `web/tests/users-page.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { canDeleteUser, canDisableUser } from "@/lib/auth";
import type { DashboardUser } from "@/lib/auth";

function user(id: string, enabled = true): DashboardUser {
  return {
    id,
    username: id,
    displayName: id,
    role: "admin",
    enabled,
    createdAt: 1,
    updatedAt: 1,
    lastLoginAt: null,
  };
}

describe("user admin safety rules", () => {
  it("does not allow deleting the current user", () => {
    expect(canDeleteUser(user("u1"), "u1", [user("u1"), user("u2")])).toBe(false);
  });

  it("does not allow deleting or disabling the last enabled admin", () => {
    expect(canDeleteUser(user("u1"), "u2", [user("u1")])).toBe(false);
    expect(canDisableUser(user("u1"), [user("u1")])).toBe(false);
  });

  it("allows changing a non-current admin when another admin remains enabled", () => {
    expect(canDeleteUser(user("u2"), "u1", [user("u1"), user("u2")])).toBe(true);
    expect(canDisableUser(user("u2"), [user("u1"), user("u2")])).toBe(true);
  });
});
```

Run: `cd web && npm test -- users-page.test.ts`

Expected: FAIL because helpers do not exist.

- [ ] **Step 2: Implement pure safety helpers**

Modify `web/src/lib/auth.ts`:

```ts
export function enabledAdminCount(users: DashboardUser[]): number {
  return users.filter((u) => u.enabled && u.role === "admin").length;
}

export function canDeleteUser(user: DashboardUser, currentUserId: string | null, users: DashboardUser[]): boolean {
  if (user.id === currentUserId) return false;
  if (user.enabled && user.role === "admin" && enabledAdminCount(users) <= 1) return false;
  return true;
}

export function canDisableUser(user: DashboardUser, users: DashboardUser[]): boolean {
  if (!user.enabled) return true;
  if (user.role === "admin" && enabledAdminCount(users) <= 1) return false;
  return true;
}
```

- [ ] **Step 3: Add Users tab to navigation**

Modify `web/src/components/Sidebar.tsx`:

```ts
export type TabKey =
  | "now"
  | "devices"
  | "homeAssistant"
  | "family"
  | "tasks"
  | "activity"
  | "usage"
  | "users";
```

Add tab:

```ts
{
  key: "users",
  labelKey: "nav.users",
  hintKey: "nav.usersHint",
  Icon: IconFamily,
}
```

Reusing `IconFamily` is acceptable for MVP because the visual language already maps people/family to an icon. If a dedicated user icon already exists in `navIcons.tsx`, use it instead.

Update `web/src/i18n/locales/zh/nav.json`:

```json
"users": "用户",
"usersHint": "管理登录账号"
```

Update English:

```json
"users": "Users",
"usersHint": "Manage dashboard accounts"
```

- [ ] **Step 4: Implement UsersPage**

Create `web/src/components/UsersPage.tsx`:

```tsx
import { useEffect, useState } from "react";
import {
  changeDashboardUserPassword,
  createDashboardUser,
  deleteDashboardUser,
  listDashboardUsers,
  updateDashboardUser,
} from "@/api/auth";
import type { DashboardUser } from "@/lib/auth";
import { canDeleteUser, canDisableUser, validatePasswordPair } from "@/lib/auth";
import { toast } from "./Toast";

export function UsersPage({ currentUserId }: { currentUserId: string | null }) {
  const [users, setUsers] = useState<DashboardUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  async function reload() {
    setLoading(true);
    try {
      setUsers(await listDashboardUsers());
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void reload(); }, []);

  if (loading) return <div className="p-6 text-text-tertiary">正在加载用户…</div>;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-page-title text-text-primary">用户</h1>
          <p className="text-caption text-text-tertiary">管理可以登录 Miloco dashboard 的账号。</p>
        </div>
        <button type="button" className="px-3 py-2 rounded-md bg-brand-primary text-white">添加用户</button>
      </div>
      <div className="rounded-xl border border-border bg-bg-secondary divide-y divide-border">
        {users.map((u) => (
          <div key={u.id} className="p-4 flex items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="text-title text-text-primary truncate">{u.displayName || u.username}</div>
              <div className="text-caption-mono text-text-tertiary truncate">{u.username} · {u.enabled ? "已启用" : "已停用"}</div>
            </div>
            <div className="flex items-center gap-2">
              <button type="button" className="px-2 py-1 rounded-md border border-border">改密码</button>
              <button type="button" className="px-2 py-1 rounded-md border border-border">编辑</button>
              <button
                type="button"
                disabled={!canDeleteUser(u, currentUserId, users) || busy}
                onClick={async () => {
                  if (!confirm(`删除用户 ${u.username}？`)) return;
                  setBusy(true);
                  try {
                    await deleteDashboardUser(u.id);
                    await reload();
                    toast("用户已删除", "ok");
                  } finally {
                    setBusy(false);
                  }
                }}
                className="px-2 py-1 rounded-md border border-border disabled:opacity-40"
              >
                删除
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
```

Complete the add/edit/password dialog states in the same file with controlled inputs:

- Add user calls `createDashboardUser`.
- Edit calls `updateDashboardUser`.
- Password calls `changeDashboardUserPassword`.
- Client-side password validation uses `validatePasswordPair`.
- Disable toggle uses `canDisableUser` for visual guard and backend remains authoritative.

- [ ] **Step 5: Render Users tab in `App.tsx`**

In the existing tab rendering switch:

```tsx
{activeTab === "users" && (
  <UsersPage currentUserId={auth.user?.id ?? null} />
)}
```

Import `UsersPage`.

- [ ] **Step 6: Run focused frontend tests and typecheck**

Run:

```bash
cd web
npm test -- users-page.test.ts auth-flow.test.ts
npm run typecheck
```

Expected: PASS.

- [ ] **Step 7: Commit Task 6**

```bash
git add web/src/components/UsersPage.tsx web/src/api/auth.ts web/src/lib/auth.ts \
  web/src/components/Sidebar.tsx web/src/App.tsx web/src/i18n \
  web/tests/users-page.test.ts
git commit -m "feat: add dashboard users tab"
```

---

### Task 7: CLI recovery commands and installer first-admin setup

**Files:**

- Create: `cli/src/miloco_cli/commands/auth.py`
- Modify: `cli/src/miloco_cli/main.py`
- Test: `cli/tests/test_auth_commands.py`
- Modify: `scripts/install.py`
- Modify: `scripts/i18n/zh.json`
- Modify: `scripts/i18n/en.json`

**Interfaces:**

- Consumes backend auth API.
- Produces:
  - `miloco-cli auth status`
  - `miloco-cli auth setup --username <name> --display-name <name> --password-stdin`
  - `miloco-cli auth reset-password --username <name> --password-stdin`
  - Installer step that creates the first admin interactively without putting passwords in command arguments.

- [ ] **Step 1: Add failing CLI tests**

Add `cli/tests/test_auth_commands.py`:

```python
from click.testing import CliRunner

from miloco_cli.main import cli


def test_auth_setup_requires_password_stdin(monkeypatch) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["auth", "setup", "--username", "lynx"])
    assert result.exit_code != 0
    assert "--password-stdin" in result.output


def test_auth_setup_sends_password_in_body_not_argv(monkeypatch) -> None:
    calls = []

    def fake_post(path, body=None, **kwargs):
        calls.append((path, body, kwargs))
        return {"code": 0, "message": "ok", "data": {"user": {"username": "lynx"}}}

    monkeypatch.setattr("miloco_cli.commands.auth.api_post", fake_post)
    runner = CliRunner()
    result = runner.invoke(
      cli,
      ["auth", "setup", "--username", "lynx", "--display-name", "Lynx", "--password-stdin"],
      input="correct horse battery\n",
    )
    assert result.exit_code == 0
    assert calls[0][0] == "/api/auth/setup"
    assert calls[0][1]["password"] == "correct horse battery"
    assert "correct horse battery" not in result.output
```

Run: `cd cli && uv run pytest tests/test_auth_commands.py -v`

Expected: FAIL because command does not exist.

- [ ] **Step 2: Implement CLI auth commands**

Create `cli/src/miloco_cli/commands/auth.py`:

```python
import sys

import click

from miloco_cli.client import api_get, api_post
from miloco_cli.commands._ordered_group import OrderedGroup
from miloco_cli.output import print_result


@click.group("auth", cls=OrderedGroup)
def auth_group():
    """Dashboard 用户鉴权管理。"""


def _read_password_from_stdin(password_stdin: bool) -> str:
    if not password_stdin:
        raise click.UsageError("provide --password-stdin and pipe the password on stdin")
    password = sys.stdin.readline().rstrip("\n")
    if not password:
        raise click.UsageError("password from stdin is empty")
    return password


@auth_group.command("status")
@click.option("--pretty", is_flag=True)
def auth_status(pretty: bool):
    print_result(api_get("/api/auth/status"), pretty)


@auth_group.command("setup")
@click.option("--username", required=True)
@click.option("--display-name", default="")
@click.option("--password-stdin", is_flag=True)
@click.option("--pretty", is_flag=True)
def auth_setup(username: str, display_name: str, password_stdin: bool, pretty: bool):
    password = _read_password_from_stdin(password_stdin)
    result = api_post(
        "/api/auth/setup",
        {
            "username": username,
            "display_name": display_name,
            "password": password,
            "password_confirm": password,
        },
    )
    print_result(result, pretty)
```

Add `reset-password` by looking up users from `/api/users`, matching username case-insensitively client-side, then posting `/api/users/{id}/password`.

Modify `cli/src/miloco_cli/main.py`:

```python
from miloco_cli.commands.auth import auth_group
...
cli.add_command(auth_group)
```

- [ ] **Step 3: Add installer dashboard-admin step**

In `scripts/install.py`, add a method after `_step_service` and before `_step_account`:

```python
def _step_dashboard_auth(self) -> None:
    self._step_header("dashboard_auth.title", "dashboard_auth.subtitle")
    if not self._service_started:
        self.ui.step_skip(self.ui.i18n.t("dashboard_auth.service_start_failed"))
        return
    try:
        result = subprocess.run(
            ["miloco-cli", "auth", "status"],
            check=True,
            capture_output=True,
            text=True,
        )
        status_data = json.loads(result.stdout)
        needs_setup = bool(status_data.get("data", {}).get("needs_setup"))
    except Exception:
        self.ui.step_fail(self.ui.i18n.t("dashboard_auth.status_failed"))
        return
    if not needs_setup:
        self.ui.step_ok(self.ui.i18n.t("dashboard_auth.already_configured"))
        return
    if not self.platform.is_interactive:
        self.ui.step_skip(self.ui.i18n.t("dashboard_auth.open_browser_to_setup"))
        return
    create_label = self.ui.i18n.t("dashboard_auth.create_now")
    browser_label = self.ui.i18n.t("dashboard_auth.create_in_browser")
    choice = self.ui.prompt_select(
        self.ui.i18n.t("dashboard_auth.setup_ask"),
        choices=[create_label, browser_label],
    )
    if choice != create_label:
        self.ui.step_skip(self.ui.i18n.t("dashboard_auth.open_browser_to_setup"))
        return
    username = self.ui.prompt_input(
        self.ui.i18n.t("dashboard_auth.username"),
        validate=lambda v: True if v.strip() else self.ui.i18n.t("dashboard_auth.username_required"),
    )
    display_name = self.ui.prompt_input(
        self.ui.i18n.t("dashboard_auth.display_name"),
        default=username,
    )
    password = self.ui.prompt_input(
        self.ui.i18n.t("dashboard_auth.password"),
        password=True,
        validate=lambda v: True if len(v) >= 8 else self.ui.i18n.t("dashboard_auth.password_too_short"),
    )
    confirm = self.ui.prompt_input(
        self.ui.i18n.t("dashboard_auth.password_confirm"),
        password=True,
        validate=lambda v: True if v == password else self.ui.i18n.t("dashboard_auth.password_mismatch"),
    )
    proc = subprocess.run(
        ["miloco-cli", "auth", "setup", "--username", username, "--display-name", display_name, "--password-stdin"],
        input=f"{password}\n",
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        self.ui.step_ok(self.ui.i18n.t("dashboard_auth.created"))
    else:
        self.ui.step_fail(self.ui.i18n.t("dashboard_auth.create_failed"))
```

Call `self._step_dashboard_auth()` from the installer run sequence immediately after service start. Password travels through stdin only.

- [ ] **Step 4: Add installer i18n**

Add to `scripts/i18n/zh.json`:

```json
"dashboard_auth": {
  "title": "Dashboard 登录",
  "subtitle": "设置 Miloco 网页控制台的管理员账号",
  "service_start_failed": "后端未启动，跳过 dashboard 登录设置",
  "status_failed": "无法读取 dashboard 登录状态",
  "already_configured": "dashboard 管理员已配置",
  "open_browser_to_setup": "请打开 dashboard 创建第一个管理员",
  "setup_ask": "现在创建第一个 dashboard 管理员吗？",
  "create_now": "现在创建",
  "create_in_browser": "稍后在浏览器创建",
  "username": "用户名",
  "display_name": "显示名",
  "password": "密码",
  "password_confirm": "确认密码",
  "username_required": "用户名不能为空",
  "password_too_short": "密码至少需要 8 个字符",
  "password_mismatch": "两次输入的密码不一致",
  "created": "dashboard 管理员已创建",
  "create_failed": "创建 dashboard 管理员失败"
}
```

Add English equivalent.

- [ ] **Step 5: Run CLI and installer-focused tests**

Run:

```bash
cd cli
uv run pytest tests/test_auth_commands.py tests/test_commands.py -v
cd ..
python3 -m py_compile scripts/install.py
```

Expected: PASS.

- [ ] **Step 6: Commit Task 7**

```bash
git add cli/src/miloco_cli/commands/auth.py cli/src/miloco_cli/main.py \
  cli/tests/test_auth_commands.py scripts/install.py scripts/i18n/zh.json scripts/i18n/en.json
git commit -m "feat: add dashboard auth setup tooling"
```

---

### Task 8: Full integration, docs alignment, and release readiness

**Files:**

- Modify: `README.md`
- Modify: `README.zh.md`
- Modify: `user_guide.md`
- Modify: `user_guide_zh.md`
- Modify: `web/README.md`
- Modify: `knowledge/06-dev-guide/dev-guide.md`
- Modify: `docs/2026-08-30-dashboard-auth_PROGRESS.md`
- Modify tests or code only if full-gate failures reveal a missed migration from Tasks 1-7.

**Interfaces:**

- Consumes all previous tasks.
- Produces a final locally verified source tree ready for a production CO.

- [ ] **Step 1: Add docs for the new auth boundary**

Update user-facing docs with plain-language instructions:

```markdown
### Dashboard login

Miloco now protects the web dashboard with local login accounts. On a fresh install,
open the dashboard and create the first administrator. After that, every browser
must log in before it can see cameras, devices, model settings, Home Assistant,
or logs.

CLI/OpenClaw integrations still use the local service token in `config.json`.
Do not share that token with browsers or people.
```

Add Chinese equivalent:

```markdown
### Dashboard 登录

Miloco 现在会保护网页控制台。首次安装后，打开 dashboard 创建第一个管理员；
之后每个浏览器都需要先登录，才能看到摄像头、设备、模型设置、Home Assistant
和日志。

CLI / OpenClaw 仍然使用 `config.json` 里的本机服务 token。这个 token 是机器调用用的，
不要发给浏览器或其他人。
```

- [ ] **Step 2: Remove stale frontend token-injection assumptions**

Search:

```bash
rg -n "__MILOCO_TOKEN__|__MILOCO_INJECT_TOKEN_HERE__|resolveToken\\(|token-injected|query token|window\\.__MILOCO_TOKEN__" web backend/miloco/src docs knowledge README.md README.zh.md user_guide.md user_guide_zh.md
```

Expected allowed hits:

- Historical notes in the dashboard auth design/spec are acceptable.
- Tests that assert no injection are acceptable.
- Runtime frontend code should not depend on `resolveToken()` for dashboard auth.
- Backend `spa_handler` should not replace the placeholder with `server.token`.

If runtime code still uses token injection, fix it in this task and add a focused test before the fix.

- [ ] **Step 3: Run backend auth and smoke tests**

Run:

```bash
cd backend
uv run pytest \
  miloco/tests/auth \
  miloco/tests/test_sse_auth.py \
  miloco/tests/devices/test_router.py \
  miloco/tests/test_settings.py \
  -v
```

Expected: PASS.

- [ ] **Step 4: Run CLI tests**

Run:

```bash
cd cli
uv run pytest tests/test_auth_commands.py tests/test_client.py tests/test_commands.py -v
```

Expected: PASS.

- [ ] **Step 5: Run frontend tests and build**

Run:

```bash
cd web
npm test -- auth-client.test.ts auth-flow.test.ts users-page.test.ts live-player-url.test.ts
npm run typecheck
npm run build
```

Expected: PASS.

- [ ] **Step 6: Run project-level checks**

Run:

```bash
./scripts/local-ci.sh --tests
git diff --check
```

Expected: PASS.

If `./scripts/local-ci.sh --tests` is too broad for the immediate development host due to known environmental limits, run the focused checks above, record the exact skipped gate and reason in `docs/2026-08-30-dashboard-auth_PROGRESS.md`, and do not deploy to production until an equivalent gate has passed.

- [ ] **Step 7: Manual local smoke without secrets**

Run the backend locally with a temporary `MILOCO_HOME`:

```bash
tmp_home="$(mktemp -d)"
(
  cd backend
  MILOCO_HOME="$tmp_home" \
    MILOCO_SERVER__HOST=127.0.0.1 \
    MILOCO_SERVER__PORT=1810 \
    uv run miloco-backend
)
```

In a second terminal:

```bash
curl -sS http://127.0.0.1:1810/ | grep -qv 'service-token'
curl -sS http://127.0.0.1:1810/api/auth/status
```

Expected:

- Root HTML does not contain the configured service token.
- `/api/auth/status` returns `needs_setup=true`.

Do not use real production tokens in this smoke.

- [ ] **Step 8: Update progress doc**

Update `docs/2026-08-30-dashboard-auth_PROGRESS.md`:

```markdown
## 2026-08-30 HH:MM HKT

- Current work: Completed local implementation and verification for native dashboard auth.
- Expected result: Source tree is ready for production CO deployment.
- Result: Achieved / Partial, with exact test command evidence.
- Next step: Open production CO for `miloco.esxi`, back up runtime data, deploy exact source SHA, and verify login/setup behavior.
```

Use the actual timestamp and actual result.

- [ ] **Step 9: Commit Task 8**

```bash
git add README.md README.zh.md user_guide.md user_guide_zh.md web/README.md \
  knowledge/06-dev-guide/dev-guide.md docs/2026-08-30-dashboard-auth_PROGRESS.md
git commit -m "docs: document dashboard authentication"
```

- [ ] **Step 10: Push the completed implementation branch**

Use the openclaw-co identity already proven for this repository:

```bash
GIT_SSH_COMMAND='ssh -i /Users/nicholasliao/.ssh/id_co_openclaw -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new' git push
```

Expected: remote `main` advances to the final dashboard-auth source SHA.

---

## Production Deployment Plan After Implementation

Do not start production deployment until all implementation tasks above are committed and pushed.

- [ ] Open a Software CO for `miloco.esxi`.
- [ ] Record exact source SHA in the CO payload.
- [ ] Take a pre-deploy backup of Miloco runtime data on `miloco.esxi`.
- [ ] Deploy using the project's approved deployment path, not a one-off manual copy.
- [ ] Verify `/health` returns OK.
- [ ] Verify unauthenticated `/` HTML does not contain `server.token`.
- [ ] Verify an unauthenticated browser shows setup or login, not the dashboard.
- [ ] Create or log in as the first dashboard admin.
- [ ] Verify dashboard loads after login.
- [ ] Verify the top user menu shows the logged-in dashboard user and logout works.
- [ ] Verify after logout, business APIs return 401 from the browser session.
- [ ] Verify service-token CLI/OpenClaw calls still work.
- [ ] Verify RTSP live view, Omni config, Home Assistant, perception logs, and Users tab after login.
- [ ] Close the CO with exact deployed SHA and credential-safe evidence.

---

## Self-Review Checklist

- Spec coverage: Tasks 1-8 cover first-admin setup, local users, password hashing, sessions, CSRF, no token injection, media/SSE/WebSocket auth, login UI, user menu, Users tab, CLI/install, docs, and production verification.
- Forbidden-marker scan: The only scan hit after review is the intentional Chinese UI copy `稍后在浏览器创建`; no executable step asks the implementer to fill in missing work later.
- Type consistency: Backend auth interfaces use `DashboardUserPublic`, `DashboardUserRecord`, `DashboardSessionRecord`, `AuthStatusData`, `AuthContext`, and the cookie/header names defined in Tasks 1-3; frontend interfaces use `DashboardUser`, `AuthStatus`, and `setCsrfToken` defined in Task 5.
- Scope check: SSO, MFA, fine-grained roles, service-token rotation, Xiaomi OAuth changes, RTSP credential changes, Omni behavior changes, and HA behavior changes remain out of scope.
