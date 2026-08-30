from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from miloco.auth.schema import DashboardSessionRecord, DashboardUserRecord
from miloco.database.connector import get_db_connector
from miloco.utils.time_utils import now_ms


def normalize_username(username: str) -> str:
    return username.strip().casefold()


class DashboardAuthRepo:
    def __init__(self) -> None:
        self.db = get_db_connector()

    @staticmethod
    def _row_to_user(row: dict[str, Any]) -> DashboardUserRecord:
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

    @staticmethod
    def _row_to_session(row: dict[str, Any]) -> DashboardSessionRecord:
        return DashboardSessionRecord(**row)

    @staticmethod
    def _user_from_connection(conn: sqlite3.Connection, user_id: str) -> DashboardUserRecord | None:
        row = conn.execute(
            "SELECT * FROM dashboard_user WHERE id = ?", (user_id,)
        ).fetchone()
        return DashboardAuthRepo._row_to_user(dict(row)) if row is not None else None

    @staticmethod
    def _insert_user(
        conn: sqlite3.Connection,
        username: str,
        display_name: str,
        password_hash: str,
        role: str = "admin",
        enabled: bool = True,
    ) -> DashboardUserRecord:
        username_clean = username.strip()
        username_norm = normalize_username(username)
        if not username_norm:
            raise ValueError("username_required")
        if role != "admin":
            raise ValueError("role_not_supported")

        timestamp = now_ms()
        user_id = uuid.uuid4().hex
        try:
            conn.execute(
                """
                INSERT INTO dashboard_user
                  (id, username, username_norm, display_name, password_hash,
                   role, enabled, created_at, updated_at, last_login_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    user_id,
                    username_clean,
                    username_norm,
                    display_name.strip(),
                    password_hash,
                    role,
                    int(enabled),
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("username_exists") from exc
        user = DashboardAuthRepo._user_from_connection(conn, user_id)
        assert user is not None
        return user

    def any_user_exists(self) -> bool:
        rows = self.db.execute_query("SELECT COUNT(*) AS n FROM dashboard_user")
        return bool(rows and rows[0]["n"] > 0)

    def any_enabled_user(self) -> bool:
        rows = self.db.execute_query(
            "SELECT COUNT(*) AS n FROM dashboard_user WHERE enabled = 1"
        )
        return bool(rows and rows[0]["n"] > 0)

    def count_enabled_admins(self, exclude_user_id: str | None = None) -> int:
        if exclude_user_id is not None:
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
        username_clean = username.strip()
        username_norm = normalize_username(username)
        if not username_norm:
            raise ValueError("username_required")
        if role != "admin":
            raise ValueError("role_not_supported")

        timestamp = now_ms()
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
                    username_clean,
                    username_norm,
                    display_name.strip(),
                    password_hash,
                    role,
                    int(enabled),
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("username_exists") from exc
        user = self.get_user_by_id(user_id)
        assert user is not None
        return user

    def create_first_admin(
        self, username: str, display_name: str, password_hash: str
    ) -> DashboardUserRecord:
        with self.db.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute("SELECT 1 FROM dashboard_user LIMIT 1").fetchone():
                    raise ValueError("setup_completed")
                user = self._insert_user(conn, username, display_name, password_hash)
                conn.commit()
                return user
            except Exception:
                conn.rollback()
                raise

    def get_user_by_id(self, user_id: str) -> DashboardUserRecord | None:
        rows = self.db.execute_query(
            "SELECT * FROM dashboard_user WHERE id = ?", (user_id,)
        )
        return self._row_to_user(rows[0]) if rows else None

    def get_user_by_username(self, username: str) -> DashboardUserRecord | None:
        rows = self.db.execute_query(
            "SELECT * FROM dashboard_user WHERE username_norm = ?",
            (normalize_username(username),),
        )
        return self._row_to_user(rows[0]) if rows else None

    def list_users(self) -> list[DashboardUserRecord]:
        rows = self.db.execute_query(
            "SELECT * FROM dashboard_user ORDER BY created_at ASC, id ASC"
        )
        return [self._row_to_user(row) for row in rows]

    def update_user(
        self,
        user_id: str,
        *,
        username: str | None = None,
        display_name: str | None = None,
        enabled: bool | None = None,
    ) -> DashboardUserRecord:
        assignments: list[str] = []
        params: list[Any] = []
        if username is not None:
            username_clean = username.strip()
            username_norm = normalize_username(username)
            if not username_norm:
                raise ValueError("username_required")
            assignments.extend(("username = ?", "username_norm = ?"))
            params.extend((username_clean, username_norm))
        if display_name is not None:
            assignments.append("display_name = ?")
            params.append(display_name.strip())
        if enabled is not None:
            assignments.append("enabled = ?")
            params.append(int(enabled))

        if not assignments:
            user = self.get_user_by_id(user_id)
            if user is None:
                raise ValueError("user_not_found")
            return user

        assignments.append("updated_at = ?")
        params.extend((now_ms(), user_id))
        try:
            changed = self.db.execute_update(
                f"UPDATE dashboard_user SET {', '.join(assignments)} WHERE id = ?",
                tuple(params),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("username_exists") from exc
        if changed == 0:
            raise ValueError("user_not_found")
        user = self.get_user_by_id(user_id)
        assert user is not None
        return user

    def update_user_guarded(
        self,
        user_id: str,
        *,
        username: str | None = None,
        display_name: str | None = None,
        enabled: bool | None = None,
    ) -> DashboardUserRecord:
        with self.db.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._user_from_connection(conn, user_id)
                if existing is None:
                    raise ValueError("user_not_found")
                if enabled is False and existing.enabled:
                    count = conn.execute(
                        "SELECT COUNT(*) FROM dashboard_user "
                        "WHERE enabled = 1 AND role = 'admin' AND id != ?",
                        (user_id,),
                    ).fetchone()[0]
                    if count == 0:
                        raise ValueError("last_admin")

                assignments: list[str] = []
                params: list[Any] = []
                if username is not None:
                    username_clean = username.strip()
                    username_norm = normalize_username(username)
                    if not username_norm:
                        raise ValueError("username_required")
                    assignments.extend(("username = ?", "username_norm = ?"))
                    params.extend((username_clean, username_norm))
                if display_name is not None:
                    assignments.append("display_name = ?")
                    params.append(display_name.strip())
                if enabled is not None:
                    assignments.append("enabled = ?")
                    params.append(int(enabled))
                if assignments:
                    assignments.append("updated_at = ?")
                    params.extend((now_ms(), user_id))
                    try:
                        conn.execute(
                            f"UPDATE dashboard_user SET {', '.join(assignments)} WHERE id = ?",
                            tuple(params),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise ValueError("username_exists") from exc
                if enabled is False:
                    conn.execute("DELETE FROM dashboard_session WHERE user_id = ?", (user_id,))
                user = self._user_from_connection(conn, user_id)
                assert user is not None
                conn.commit()
                return user
            except Exception:
                conn.rollback()
                raise

    def update_password(self, user_id: str, password_hash: str) -> DashboardUserRecord:
        changed = self.db.execute_update(
            "UPDATE dashboard_user SET password_hash = ?, updated_at = ? WHERE id = ?",
            (password_hash, now_ms(), user_id),
        )
        if changed == 0:
            raise ValueError("user_not_found")
        user = self.get_user_by_id(user_id)
        assert user is not None
        return user

    def update_password_and_revoke_sessions(
        self, user_id: str, password_hash: str
    ) -> DashboardUserRecord:
        with self.db.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                changed = conn.execute(
                    "UPDATE dashboard_user SET password_hash = ?, updated_at = ? WHERE id = ?",
                    (password_hash, now_ms(), user_id),
                ).rowcount
                if changed == 0:
                    raise ValueError("user_not_found")
                conn.execute("DELETE FROM dashboard_session WHERE user_id = ?", (user_id,))
                user = self._user_from_connection(conn, user_id)
                assert user is not None
                conn.commit()
                return user
            except Exception:
                conn.rollback()
                raise

    def delete_user(self, user_id: str) -> bool:
        return self.db.execute_update(
            "DELETE FROM dashboard_user WHERE id = ?", (user_id,)
        ) > 0

    def delete_user_guarded(self, user_id: str) -> None:
        with self.db.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                user = self._user_from_connection(conn, user_id)
                if user is None:
                    raise ValueError("user_not_found")
                if user.enabled:
                    count = conn.execute(
                        "SELECT COUNT(*) FROM dashboard_user "
                        "WHERE enabled = 1 AND role = 'admin' AND id != ?",
                        (user_id,),
                    ).fetchone()[0]
                    if count == 0:
                        raise ValueError("last_admin")
                conn.execute("DELETE FROM dashboard_session WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM dashboard_user WHERE id = ?", (user_id,))
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def create_session(
        self,
        user_id: str,
        session_hash: str,
        csrf_hash: str,
        expires_at: int,
        user_agent_hash: str,
        client_ip_hint: str | None,
    ) -> DashboardSessionRecord:
        timestamp = now_ms()
        session_id = uuid.uuid4().hex
        self.db.execute_update(
            """
            INSERT INTO dashboard_session
              (id, user_id, session_hash, csrf_hash, created_at, expires_at,
               last_seen_at, user_agent_hash, client_ip_hint)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                user_id,
                session_hash,
                csrf_hash,
                timestamp,
                expires_at,
                timestamp,
                user_agent_hash,
                client_ip_hint,
            ),
        )
        rows = self.db.execute_query(
            "SELECT * FROM dashboard_session WHERE id = ?", (session_id,)
        )
        assert rows
        return self._row_to_session(rows[0])

    def get_session_by_hash(
        self, session_hash: str, now_ms: int
    ) -> DashboardSessionRecord | None:
        rows = self.db.execute_query(
            "SELECT * FROM dashboard_session "
            "WHERE session_hash = ? AND expires_at > ?",
            (session_hash, now_ms),
        )
        return self._row_to_session(rows[0]) if rows else None

    def delete_session(self, session_id: str) -> bool:
        return self.db.execute_update(
            "DELETE FROM dashboard_session WHERE id = ?", (session_id,)
        ) > 0

    def delete_sessions_for_user(self, user_id: str) -> int:
        return self.db.execute_update(
            "DELETE FROM dashboard_session WHERE user_id = ?", (user_id,)
        )

    def delete_expired_sessions(self, now_ms: int) -> int:
        return self.db.execute_update(
            "DELETE FROM dashboard_session WHERE expires_at <= ?", (now_ms,)
        )

    def touch_session(self, session_id: str, timestamp: int) -> bool:
        return (
            self.db.execute_update(
                "UPDATE dashboard_session SET last_seen_at = ? WHERE id = ?",
                (timestamp, session_id),
            )
            > 0
        )

    def touch_user_login(self, user_id: str, timestamp: int) -> bool:
        return (
            self.db.execute_update(
                "UPDATE dashboard_user SET last_login_at = ?, updated_at = ? WHERE id = ?",
                (timestamp, timestamp, user_id),
            )
            > 0
        )

    def update_session_csrf(
        self, session_id: str, csrf_hash: str, timestamp: int
    ) -> bool:
        return (
            self.db.execute_update(
                "UPDATE dashboard_session SET csrf_hash = ?, last_seen_at = ? WHERE id = ?",
                (csrf_hash, timestamp, session_id),
            )
            > 0
        )
