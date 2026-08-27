# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Shared configuration persistence tests."""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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


def test_concurrent_backend_updates_share_one_read_modify_write_lock(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"existing": {"kept": True}}))
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    reset_settings()
    start = threading.Barrier(3)
    active_reads = 0
    max_active_reads = 0
    counter_lock = threading.Lock()
    original_read = agent_config._read_config_dict

    def slow_read(path: Path):
        nonlocal active_reads, max_active_reads
        with counter_lock:
            active_reads += 1
            max_active_reads = max(max_active_reads, active_reads)
        time.sleep(0.05)
        try:
            return original_read(path)
        finally:
            with counter_lock:
                active_reads -= 1

    monkeypatch.setattr(agent_config, "_read_config_dict", slow_read)

    def update_server() -> None:
        start.wait()
        agent_config.update_shared_config(server={"token": "server-value"})

    def update_camera() -> None:
        start.wait()
        agent_config.update_shared_config(camera={"rtsp_sources": []})

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(update_server)
        second = pool.submit(update_camera)
        start.wait()
        first.result()
        second.result()

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert max_active_reads == 1
    assert saved == {
        "existing": {"kept": True},
        "server": {"token": "server-value"},
        "camera": {"rtsp_sources": []},
    }
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "config.json.lock").stat().st_mode) == 0o600
    reset_settings()


def test_locked_rtsp_mutation_preserves_other_config_and_sources(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "server": {"token": "keep-token"},
                "camera": {
                    "frame_interval": 700,
                    "rtsp_sources": [{"id": "first"}],
                },
            }
        )
    )
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    reset_settings()

    merged = agent_config.mutate_rtsp_sources(
        lambda sources: [*sources, {"id": "second"}]
    )

    assert merged == {
        "server": {"token": "keep-token"},
        "camera": {
            "frame_interval": 700,
            "rtsp_sources": [{"id": "first"}, {"id": "second"}],
        },
    }
    assert json.loads(config_path.read_text(encoding="utf-8")) == merged
    reset_settings()
