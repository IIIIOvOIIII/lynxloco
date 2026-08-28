"""Executable contract for the future ai-lab deployment CLI.

The deployment script is deliberately introduced by a later task.  These tests
activate their CLI assertions as soon as ``deploy.sh`` exists, while keeping
the allowlist contract independently enforceable from this first commit.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


DEPLOY_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = DEPLOY_DIR.parents[1]
DEPLOY_SCRIPT = DEPLOY_DIR / "deploy.sh"
ALLOWLIST = DEPLOY_DIR / "artifact-files.txt"

EXPECTED_COMMANDS = {"build", "preflight", "deploy", "verify", "status", "rollback"}
EXPECTED_COMMAND_ORDER = ("build", "preflight", "deploy", "verify", "status", "rollback")
ALLOWED_HOSTS = {"ai-lab01.esxi", "ai-lab02.esxi"}
FORBIDDEN_PARTS = {".git", ".env", "config.json", ".venv", "node_modules", "__pycache__"}
EXTERNAL_COMMANDS = (
    "ssh",
    "scp",
    "sftp",
    "rsync",
    "tar",
    "docker",
    "podman",
    "nerdctl",
    "buildctl",
    "build",
    "python",
    "python3",
    "pip",
    "pip3",
    "uv",
    "make",
    "curl",
    "wget",
    "rclone",
    "oras",
    "skopeo",
)


def _allowlist_entries() -> set[str]:
    return {
        line.strip()
        for line in ALLOWLIST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def _write_stub(path: Path, name: str, log: Path, body: str = "exit 0") -> None:
    command = path / name
    command.write_text(
        "#!/usr/bin/env sh\n"
        f"printf '%s' '{name}' >> '{log}'\n"
        f"printf ' %s' \"$@\" >> '{log}'\n"
        f"printf '\\n' >> '{log}'\n"
        f"{body}\n",
        encoding="utf-8",
    )
    command.chmod(command.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def command_stubs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Record external command names without ever recording environment data."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    call_log = tmp_path / "calls.log"
    for command in EXTERNAL_COMMANDS:
        _write_stub(bin_dir, command, call_log)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return call_log


def _run_deploy(*arguments: str) -> subprocess.CompletedProcess[str]:
    _assert_deferred_cli_is_safe_to_execute()
    sandbox_exec = shutil.which("sandbox-exec")
    assert sandbox_exec, "refusing to run deferred deploy CLI tests without a network-denying sandbox"
    return subprocess.run(
        [sandbox_exec, "-p", "(version 1) (allow default) (deny network*)", str(DEPLOY_SCRIPT), *arguments],
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "DOCKER_HOST": "unix:///dev/null",
            "CONTAINER_HOST": "unix:///dev/null",
            "PODMAN_HOST": "unix:///dev/null",
        },
    )


def _require_deploy_script() -> None:
    if not DEPLOY_SCRIPT.exists():
        pytest.skip("deploy.sh is supplied by the following deployment task")


def _assert_deferred_cli_is_safe_to_execute() -> None:
    """Reject tool paths that would evade the temporary-PATH command stubs."""
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    command_pattern = "|".join(re.escape(command) for command in EXTERNAL_COMMANDS)
    direct_paths = re.findall(
        rf"(?<![A-Za-z0-9_.-])/(?:[A-Za-z0-9_.-]+/)*(?:{command_pattern})(?![A-Za-z0-9_.-])",
        script,
    )
    virtualenv_paths = re.findall(
        rf"(?<![A-Za-z0-9_.-])(?:\./)?(?:[A-Za-z0-9_.-]+/)*\.venv/bin/(?:{command_pattern})(?![A-Za-z0-9_.-])",
        script,
    )
    assert not direct_paths and not virtualenv_paths, (
        "deferred deploy CLI tests refuse direct tool paths that evade the isolated PATH: "
        f"{direct_paths + virtualenv_paths}"
    )


def _help_operations(help_text: str) -> set[str]:
    expected_help = [
        "Usage: deploy.sh <operation> [--host HOST]",
        "",
        "Operations:",
        *(f"  {command}" for command in EXPECTED_COMMAND_ORDER),
    ]
    assert help_text.splitlines() == expected_help, (
        "--help must use the authoritative complete command list without extra advertised operations"
    )
    return set(EXPECTED_COMMAND_ORDER)


def test_artifact_manifest_is_the_exact_release_allowlist() -> None:
    """Catches release payload expansion beyond the audited deployment contract."""
    assert _allowlist_entries() == {
        "Dockerfile",
        "compose.yaml",
        "container-entrypoint.sh",
        "remote-release.sh",
        "acceptance/",
        "requirements/backend.txt",
        "requirements/cli.txt",
        "requirements/acceptance.txt",
        "wheels/miloco-*.whl",
        "wheels/miloco_cli-*.whl",
        "wheels/miloco_miot-*-manylinux_2_28_x86_64.whl",
        "models/miloco-models-*.tar.gz",
        "release.json",
        "SHA256SUMS",
    }


def test_artifact_manifest_never_admits_forbidden_path_components() -> None:
    """Catches accidental inclusion of source control, local state, or secrets."""
    forbidden_entries = {
        entry
        for entry in _allowlist_entries()
        if FORBIDDEN_PARTS.intersection(Path(entry.rstrip("/")).parts)
    }
    assert forbidden_entries == set()


def test_help_exposes_only_the_release_operations() -> None:
    """Catches an undocumented or missing deploy CLI operation."""
    _require_deploy_script()
    result = _run_deploy("--help")
    assert result.returncode == 0, result.stderr
    assert _help_operations(result.stdout) == EXPECTED_COMMANDS
    with pytest.raises(AssertionError):
        _help_operations(result.stdout + "\nUsage: deploy.sh destroy\n")


def test_unknown_host_exits_two_before_ssh(command_stubs: Path) -> None:
    """Catches host validation that occurs after a remote connection attempt."""
    _require_deploy_script()
    result = _run_deploy("preflight", "--host", "outside-ai-lab.esxi")
    assert result.returncode == 2
    assert not command_stubs.exists(), "unknown hosts must not invoke SSH, tar, or a build command"


def test_dirty_worktree_exits_three_before_build_or_transfer(
    tmp_path: Path, command_stubs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches artifact creation or transfer before the dirty-tree safety gate."""
    _require_deploy_script()
    bin_dir = tmp_path / "git-bin"
    bin_dir.mkdir()
    git_log = tmp_path / "git-calls.log"
    _write_stub(
        bin_dir,
        "git",
        git_log,
        "case \"$*\" in *status*) printf ' M deploy/ai-lab/deploy.sh\\n' ;; *diff*) exit 1 ;; *--show-toplevel*) printf '%s\\n' \"$PWD\" ;; *rev-parse*) printf '0123456789abcdef0123456789abcdef01234567\\n' ;; esac",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    result = _run_deploy("build", "--host", "ai-lab01.esxi")
    assert result.returncode == 3
    assert not command_stubs.exists(), "a dirty tree must stop before tar, SSH, or the build command"
