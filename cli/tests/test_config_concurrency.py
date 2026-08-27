"""Cross-process shared config writer coordination tests."""

from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
import queue
import stat
from pathlib import Path

import pytest

from miloco_cli import config


def _paused_cli_set(
    home: str,
    snapshot_ready,
    release_write,
    completed,
    errors,
) -> None:
    """Pause a real CLI writer immediately before its atomic publish."""
    os.environ["MILOCO_HOME"] = home
    from miloco_cli import config as child_config

    original_atomic_write = child_config.atomic_write

    def paused_atomic_write(path: Path, data: dict) -> None:
        snapshot_ready.set()
        if not release_write.wait(10):
            raise TimeoutError("test did not release CLI config writer")
        original_atomic_write(path, data)

    setattr(child_config, "atomic_write", paused_atomic_write)
    try:
        child_config.set_value("server.url", "http://cli-writer:1810")
    except BaseException as error:  # pragma: no cover - reported to parent
        errors.put(f"{type(error).__name__}: {error}")
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


def _backend_compatible_camera_write(config_path: Path) -> None:
    lock_path = config_path.with_name(f"{config_path.name}.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        _write_camera_while_locked(config_path)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_cli_and_backend_writers_preserve_disjoint_updates(tmp_path: Path) -> None:
    """CLI must lock before re-read so a concurrent camera update is never lost."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"server": {"token": "keep-token"}}), encoding="utf-8"
    )
    context = multiprocessing.get_context("spawn")
    snapshot_ready = context.Event()
    release_write = context.Event()
    completed = context.Event()
    errors = context.Queue()
    process = context.Process(
        target=_paused_cli_set,
        args=(
            str(tmp_path),
            snapshot_ready,
            release_write,
            completed,
            errors,
        ),
    )
    process.start()

    acquired_during_cli_write = False
    lock_path = config_path.with_name(f"{config_path.name}.lock")
    fd: int | None = None
    try:
        assert snapshot_ready.wait(10), "CLI writer never reached publish boundary"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired_during_cli_write = True
            _write_camera_while_locked(config_path)
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            fd = None
        except BlockingIOError:
            assert fd is not None
            os.close(fd)
            fd = None

        release_write.set()
        assert completed.wait(10), "CLI writer did not complete"
        process.join(timeout=10)
        assert process.exitcode == 0

        if not acquired_during_cli_write:
            _backend_compatible_camera_write(config_path)
    finally:
        release_write.set()
        if fd is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)

    try:
        child_error = errors.get_nowait()
    except queue.Empty:
        child_error = None
    assert child_error is None
    assert acquired_during_cli_write is False
    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "server": {
            "token": "keep-token",
            "url": "http://cli-writer:1810",
        },
        "camera": {"rtsp_sources": [{"id": "rtsp:backend-added"}]},
    }
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


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
