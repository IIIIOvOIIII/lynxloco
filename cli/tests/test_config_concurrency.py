"""Cross-process shared config writer coordination tests."""

from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
import stat
from pathlib import Path

import pytest

from miloco_cli import config


def _observed_cli_set(
    home: str,
    started,
    read_completed,
    continue_after_read,
    read_count,
    completed,
) -> None:
    """Observe and pause the CLI's real config read in a spawned process."""
    os.environ["MILOCO_HOME"] = home
    from miloco_cli import config as child_config

    original_read_raw = child_config._read_raw

    def observed_read_raw() -> dict:
        data = original_read_raw()
        with read_count.get_lock():
            read_count.value += 1
        read_completed.set()
        if not continue_after_read.wait(10):
            raise TimeoutError("test did not release CLI config reader")
        return data

    setattr(child_config, "_read_raw", observed_read_raw)
    started.set()
    try:
        child_config.set_value("server.url", "http://cli-writer:1810")
    finally:
        completed.set()


def _write_camera_while_locked(config_path: Path) -> None:
    try:
        current = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        current = {}
    camera = current.setdefault("camera", {})
    camera["rtsp_sources"] = [{"id": "rtsp:backend-added"}]
    config.atomic_write(config_path, current)


def test_cli_and_backend_writers_preserve_disjoint_updates(tmp_path: Path) -> None:
    """CLI must not read until the backend's locked camera update is visible."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"server": {"token": "keep-token"}}), encoding="utf-8"
    )
    context = multiprocessing.get_context("spawn")
    started = context.Event()
    read_completed = context.Event()
    continue_after_read = context.Event()
    read_count = context.Value("i", 0)
    completed = context.Event()
    lock_path = config_path.with_name(f"{config_path.name}.lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(lock_fd, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    lock_held = True
    process = context.Process(
        target=_observed_cli_set,
        args=(
            str(tmp_path),
            started,
            read_completed,
            continue_after_read,
            read_count,
            completed,
        ),
    )
    process.start()

    try:
        assert started.wait(10), "CLI writer did not start"
        assert not read_completed.wait(0.25), (
            "CLI read config while backend still held the shared lock"
        )
        assert read_count.value == 0

        _write_camera_while_locked(config_path)
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_held = False
        os.close(lock_fd)

        assert read_completed.wait(10), "CLI did not read after backend released lock"
        assert read_count.value == 1
        continue_after_read.set()
        assert completed.wait(10), "CLI writer did not complete"
        process.join(timeout=10)
        assert process.exitcode == 0
    finally:
        continue_after_read.set()
        if lock_held:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        if process.is_alive():
            process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)

    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "server": {
            "token": "keep-token",
            "url": "http://cli-writer:1810",
        },
        "camera": {"rtsp_sources": [{"id": "rtsp:backend-added"}]},
    }
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


def test_config_rereads_and_writes_only_while_exclusive_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin the lock/read/write/unlock order independently of process timing."""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    events: list[str] = []
    lock_state = {"held": False}
    persisted: dict = {}

    def tracked_flock(_fd: int, operation: int) -> None:
        if operation == fcntl.LOCK_EX:
            assert lock_state["held"] is False
            lock_state["held"] = True
            events.append("lock")
        elif operation == fcntl.LOCK_UN:
            assert lock_state["held"] is True
            lock_state["held"] = False
            events.append("unlock")
        else:  # pragma: no cover - guards unexpected lock protocol changes
            pytest.fail(f"unexpected flock operation: {operation}")

    def observed_read_raw() -> dict:
        assert lock_state["held"] is True, "config was reread before LOCK_EX"
        events.append("read")
        return {
            "server": {"token": "keep-token"},
            "camera": {"rtsp_sources": [{"id": "rtsp:backend-added"}]},
        }

    def observed_atomic_write(_path: Path, data: dict) -> None:
        assert lock_state["held"] is True, "config was written after LOCK_UN"
        events.append("write")
        persisted.update(data)

    monkeypatch.setattr(config.fcntl, "flock", tracked_flock)
    monkeypatch.setattr(config, "_read_raw", observed_read_raw)
    monkeypatch.setattr(config, "atomic_write", observed_atomic_write)

    assert config.set_value("server.url", "http://cli-writer:1810") == (
        "http://cli-writer:1810"
    )

    assert events == ["lock", "read", "write", "unlock"]
    assert lock_state["held"] is False
    assert persisted == {
        "server": {
            "token": "keep-token",
            "url": "http://cli-writer:1810",
        },
        "camera": {"rtsp_sources": [{"id": "rtsp:backend-added"}]},
    }


@pytest.mark.parametrize(
    "write_error",
    [OSError("synthetic write failure"), KeyboardInterrupt("synthetic cancellation")],
    ids=["exception", "cancellation"],
)
def test_config_write_failure_releases_lock_and_preserves_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write_error: BaseException,
) -> None:
    config_path = tmp_path / "config.json"
    original = {"server": {"token": "unchanged"}}
    config_path.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))

    def fail_write(_path: Path, _data: dict) -> None:
        raise write_error

    monkeypatch.setattr(config, "atomic_write", fail_write)
    with pytest.raises(type(write_error), match=str(write_error)):
        config.set_value("server.url", "http://not-persisted:1810")

    assert json.loads(config_path.read_text(encoding="utf-8")) == original
    lock_path = config_path.with_name(f"{config_path.name}.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
