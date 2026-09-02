"""Shared nested config helpers for ``$MILOCO_HOME/config.json``.

Shared config is a multi-writer store: any of the collaborating components
can update it independently —

- CLI   : ``miloco-cli config set <path> <value>`` writes arbitrary schema
          paths (``server.url``, ``model.omni.api_key``, ``server.token``, ...).
- Plugin: the openclaw plugin writes fields it owns (e.g.
          ``agent.webhook_url``, ``agent.auth_bearer``).
- Backend: ``ensure_backend_token()`` persists ``server.token``; other
           backend-side writes go through :func:`update_shared_config`.

Backend writers using this module deep-merge through one process lock plus a
shared Unix lock file and publish via atomic ``tmpfile + os.replace``. The lock
does not turn uncooperative manual edits or legacy CLI/plugin writers into a
cross-process compare-and-swap protocol; those remain outside this guarantee.

Backend token bootstrap priority (see :func:`ensure_backend_token`):
  ``MILOCO_SERVER__TOKEN`` env / ``settings.server.token`` (already loaded)
  > existing ``config.json`` token (stable across restarts, and respects
    values written by CLI/backend)
  > new UUID (first boot, no writer has claimed the token yet).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import threading
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from miloco.config import get_settings, reset_settings
from miloco.utils.common import deep_merge
from miloco.utils.paths import config_file

logger = logging.getLogger(__name__)

_CONFIG_THREAD_LOCK = threading.RLock()


def _user_config_path() -> Path:
    """Return ``$MILOCO_HOME/config.json`` (single source of shared config)."""
    return config_file()


def _read_config_dict(path: Path) -> dict[str, Any]:
    """Read existing config from disk, returning {} on any parse error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically via tmpfile + ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    os.fchmod(fd, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextmanager
def _shared_config_lock(path: Path):
    """Serialize cooperating backend writers in this process and across processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    with _CONFIG_THREAD_LOCK:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _mutate_shared_config(
    mutation: Callable[[dict[str, Any]], dict[str, Any] | None],
) -> dict[str, Any] | None:
    path = _user_config_path()
    with _shared_config_lock(path):
        existing = _read_config_dict(path)
        merged = mutation(existing)
        if merged is None:
            return None
        _atomic_write_json(path, merged)
        reset_settings()
        return merged


def mutate_shared_config(
    mutation: Callable[[dict[str, Any]], dict[str, Any] | None],
) -> dict[str, Any] | None:
    """Apply a conditional config mutation while holding the shared writer lock.

    Returning ``None`` leaves the current file unchanged. Callers that need a
    read-modify-write decision must make that decision inside ``mutation``.
    """
    return _mutate_shared_config(mutation)


def ensure_backend_token() -> str:
    """Resolve the backend bearer token and persist it under ``server.token``.

    Returns the resolved token. Called once during bootstrap.
    """
    path = _user_config_path()
    existing = _read_config_dict(path)

    settings_token = get_settings().server.token
    if settings_token:
        token = settings_token
    else:
        existing_token = (
            existing.get("server", {}).get("token")
            if isinstance(existing.get("server"), dict)
            else None
        )
        token = existing_token or str(uuid.uuid4())

    persisted = (
        existing.get("server", {}).get("token")
        if isinstance(existing.get("server"), dict)
        else None
    )
    if persisted != token:
        update_shared_config(server={"token": token})
        logger.info("Persisted backend token to %s", path)

    return token


def update_shared_config(**updates: Any) -> dict[str, Any]:
    """Deep-merge ``updates`` into ``$MILOCO_HOME/config.json`` and persist.

    Cooperating backend writers share a process lock and a Unix flock. The file
    is re-read only after both locks are held, so updates to disjoint fields are
    not lost.
    """
    result = mutate_shared_config(lambda existing: deep_merge(existing, updates))
    assert result is not None
    return result


def mutate_rtsp_sources(
    mutation: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
) -> dict[str, Any]:
    """Mutate the current on-disk RTSP list under the shared writer lock."""

    def apply(existing: dict[str, Any]) -> dict[str, Any]:
        camera = existing.get("camera")
        raw_sources = camera.get("rtsp_sources") if isinstance(camera, dict) else []
        current = list(raw_sources) if isinstance(raw_sources, list) else []
        updated = mutation(current)
        return deep_merge(existing, {"camera": {"rtsp_sources": updated}})

    result = _mutate_shared_config(apply)
    assert result is not None
    return result
