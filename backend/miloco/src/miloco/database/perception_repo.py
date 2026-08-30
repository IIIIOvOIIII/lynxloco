"""
Perception log repo — SQLite persistence with adjacent deduplication.

Each perception cycle increments the inference counter, but only inserts
a new row when the descriptions dict differs from the previous entry.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from miloco.database.connector import get_db_connector
from miloco.utils.time_utils import now_ms

if TYPE_CHECKING:
    from miloco.perception.schema import PerceptionLogEntry

logger = logging.getLogger(__name__)


@dataclass
class PerceptionLogRuntimeStats:
    today_inference_count: int = 0
    today_insert_count: int = 0
    last_inference_ms: int | None = None
    last_insert_ms: int | None = None
    last_descriptions_empty: bool | None = None
    last_append_inserted: bool | None = None
    consecutive_empty_descriptions: int = 0
    consecutive_deduplicated: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _descriptions_empty(descriptions: dict[str, object]) -> bool:
    for value in descriptions.values():
        if isinstance(value, str):
            if value.strip():
                return False
        elif value:
            return False
    return True


class PerceptionLogRepo:
    """Data access object for the perception_log table."""

    def __init__(self):
        self.db_connector = get_db_connector()
        # In-memory dedup state
        self._last_descriptions: dict[str, str] | None = None
        # Daily inference counter (resets on date boundary)
        self._today_date: str = ""
        self._today_inference_count: int = 0
        self._runtime_stats = PerceptionLogRuntimeStats()

    def _check_date_boundary(self) -> None:
        """Reset daily inference counter if date has changed."""
        today = date.today().isoformat()
        if today != self._today_date:
            self._today_date = today
            self._today_inference_count = 0
            self._runtime_stats = PerceptionLogRuntimeStats()

    def get_today_inference_count(self) -> int:
        self._check_date_boundary()
        return self._today_inference_count

    def runtime_stats(self) -> PerceptionLogRuntimeStats:
        self._check_date_boundary()
        self._runtime_stats.today_inference_count = self._today_inference_count
        return PerceptionLogRuntimeStats(**self._runtime_stats.to_dict())

    def append(self, entry: PerceptionLogEntry) -> bool:
        """Append a perception log entry.

        Always increments inference count. Skips DB insert if descriptions
        are identical to the previous entry (adjacent dedup).

        Returns:
            True if a new row was inserted, False if deduplicated.
        """
        self._check_date_boundary()
        self._today_inference_count += 1
        self._runtime_stats.today_inference_count = self._today_inference_count
        self._runtime_stats.last_inference_ms = entry.timestamp
        empty = _descriptions_empty(entry.descriptions)
        self._runtime_stats.last_descriptions_empty = empty
        self._runtime_stats.consecutive_empty_descriptions = (
            self._runtime_stats.consecutive_empty_descriptions + 1 if empty else 0
        )

        # Adjacent dedup: skip insert if descriptions unchanged
        if (
            self._last_descriptions is not None
            and entry.descriptions == self._last_descriptions
        ):
            logger.debug("Perception log deduplicated, skipping insert: %s", entry.id)
            self._runtime_stats.last_append_inserted = False
            self._runtime_stats.consecutive_deduplicated += 1
            return False

        self._last_descriptions = entry.descriptions.copy()

        try:
            sql = """
                INSERT INTO perception_log (id, timestamp, descriptions, created_at)
                VALUES (?, ?, ?, ?)
            """
            params = (
                entry.id or str(uuid.uuid4()),
                entry.timestamp,
                json.dumps(entry.descriptions, ensure_ascii=False),
                now_ms(),
            )
            with self.db_connector.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(sql, params)
                conn.commit()

            logger.debug("Perception log inserted: %s", entry.id)
            self._runtime_stats.last_insert_ms = entry.timestamp
            self._runtime_stats.last_append_inserted = True
            self._runtime_stats.today_insert_count += 1
            self._runtime_stats.consecutive_deduplicated = 0
            return True

        except Exception as e:
            logger.error("Failed to insert perception log: %s", e)
            self._runtime_stats.last_append_inserted = False
            return False

    def query(
        self,
        after_ms: int | None = None,
        before_ms: int | None = None,
        since_ms: int | None = None,
        limit: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Query perception logs with time filters.

        Args:
            after_ms: Cursor — only return entries with timestamp > after_ms.
            before_ms: Upper bound — only return entries with timestamp < before_ms.
            since_ms: Absolute ms timestamp — only return entries with timestamp >= since_ms.
            limit: Max entries to return. None means no limit.

        Returns:
            (logs, count) where logs are dicts with "t" (ISO 8601) and "d" keys.
        """
        from miloco.utils.time_utils import ms_to_iso_local

        try:
            conditions = []
            params: list[Any] = []

            if after_ms is not None:
                conditions.append("timestamp > ?")
                params.append(after_ms)
            elif since_ms is not None:
                conditions.append("timestamp >= ?")
                params.append(since_ms)

            if before_ms is not None:
                conditions.append("timestamp < ?")
                params.append(before_ms)

            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            limit_clause = "LIMIT ?" if limit is not None else ""
            sql = f"""
                SELECT id, timestamp, descriptions
                FROM perception_log
                {where}
                ORDER BY timestamp ASC
                {limit_clause}
            """
            if limit is not None:
                params.append(limit)

            results = self.db_connector.execute_query(sql, tuple(params))

            logs = []
            for row in results:
                logs.append(
                    {
                        # 透 id 让前端 ActivityFeed 能用稳定 React key（同 rule_logs）；
                        # 否则前端走 fallback `pl_<t>_<i>` 拼装，分页 / reload 会让
                        # 同一条 perception_log（同 t 同 i）保留 React 内部 state。
                        "id": row["id"],
                        "t": ms_to_iso_local(row["timestamp"]),
                        "d": json.loads(row["descriptions"])
                        if isinstance(row["descriptions"], str)
                        else row["descriptions"],
                    }
                )

            return logs, len(logs)

        except Exception as e:
            logger.error("Failed to query perception logs: %s", e)
            return [], 0

    def count_all(self) -> int:
        """Get total count of perception log entries."""
        try:
            sql = "SELECT COUNT(*) as count FROM perception_log"
            results = self.db_connector.execute_query(sql)
            return results[0]["count"] if results else 0
        except Exception as e:
            logger.error("Failed to count perception logs: %s", e)
            return 0

    def count_since(self, since_ms: int) -> int:
        """Get count of perception log entries since an absolute Unix ms time."""
        try:
            sql = "SELECT COUNT(*) as count FROM perception_log WHERE timestamp >= ?"
            results = self.db_connector.execute_query(sql, (since_ms,))
            return results[0]["count"] if results else 0
        except Exception as e:
            logger.error("Failed to count perception logs since %s: %s", since_ms, e)
            return 0

    def latest_timestamp_ms(self) -> int | None:
        """Return the latest persisted perception log timestamp, if any."""
        try:
            sql = "SELECT MAX(timestamp) as latest_timestamp FROM perception_log"
            results = self.db_connector.execute_query(sql)
            if not results:
                return None
            value = results[0]["latest_timestamp"]
            return int(value) if value is not None else None
        except Exception as e:
            logger.error("Failed to get latest perception log timestamp: %s", e)
            return None

    def delete_before_days(self, days: int) -> int:
        """Delete perception logs older than N days.

        Returns:
            Number of deleted rows.
        """
        try:
            cutoff_ms = int((datetime.now().timestamp() - days * 86400) * 1000)
            sql = "DELETE FROM perception_log WHERE timestamp < ?"
            return self.db_connector.execute_update(sql, (cutoff_ms,))
        except Exception as e:
            logger.error("Failed to delete old perception logs: %s", e)
            return 0
