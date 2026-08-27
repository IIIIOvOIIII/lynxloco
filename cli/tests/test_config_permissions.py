"""CLI configuration persistence permissions tests."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from miloco_cli import config


def test_cli_atomic_write_replaces_world_readable_file_as_owner_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    config_path.chmod(0o644)

    original_mkstemp = config.tempfile.mkstemp

    def world_readable_mkstemp(*args, **kwargs):
        fd, tmp = original_mkstemp(*args, **kwargs)
        os.fchmod(fd, 0o644)
        return fd, tmp

    monkeypatch.setattr(config.tempfile, "mkstemp", world_readable_mkstemp)

    config.atomic_write(config_path, {"camera": {"rtsp_sources": []}})

    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
