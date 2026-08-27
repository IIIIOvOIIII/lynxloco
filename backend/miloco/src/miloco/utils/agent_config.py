"""Shared nested config helpers for ``$MILOCO_HOME/config.json``.

Shared config is a multi-writer store: any of the collaborating components
can update it independently —

- CLI   : ``miloco-cli config set <path> <value>`` writes arbitrary schema
          paths (``server.url``, ``model.omni.api_key``, ``server.token``, ...).
- Plugin: the openclaw plugin writes fields it owns (e.g.
          ``agent.webhook_url``, ``agent.auth_bearer``).
- Backend: ``ensure_backend_token()`` persists ``server.token``; other
           backend-side writes go through :func:`update_shared_config`.

All writers deep-merge into the same file via an atomic ``tmpfile +
os.replace``, so concurrent writes don't produce partial JSON. Whichever
writer publishes a field first wins, and the others pick it up on next read.

Backend token bootstrap priority (see :func:`ensure_backend_token`):
  ``MILOCO_SERVER__TOKEN`` env / ``settings.server.token`` (already loaded)
  > existing ``config.json`` token (stable across restarts, and respects
    values written by CLI/backend)
  > new UUID (first boot, no writer has claimed the token yet).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from miloco.config import get_settings, reset_settings
from miloco.utils.common import deep_merge
from miloco.utils.paths import config_file

logger = logging.getLogger(__name__)


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

    Concurrency note: The current deployment has no concurrent writers —
    bootstrap and CLI writes are serialised by install.sh / user workflow.
    If that assumption changes, add file-level locking here.
    """
    path = _user_config_path()
    existing = _read_config_dict(path)
    merged = deep_merge(existing, updates)
    _atomic_write_json(path, merged)
    reset_settings()
    return merged
