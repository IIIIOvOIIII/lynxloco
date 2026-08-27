# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Shared configuration persistence tests."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from miloco.config import reset_settings
from miloco.utils import agent_config


def test_backend_shared_config_replaces_world_readable_file_as_owner_only(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"server": {"url": "http://old"}}))
    config_path.chmod(0o644)
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    reset_settings()

    original_mkstemp = agent_config.tempfile.mkstemp

    def world_readable_mkstemp(*args, **kwargs):
        fd, tmp = original_mkstemp(*args, **kwargs)
        os.fchmod(fd, 0o644)
        return fd, tmp

    monkeypatch.setattr(agent_config.tempfile, "mkstemp", world_readable_mkstemp)

    agent_config.update_shared_config(camera={"rtsp_sources": []})

    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert json.loads(config_path.read_text(encoding="utf-8"))["camera"] == {
        "rtsp_sources": []
    }
    reset_settings()
