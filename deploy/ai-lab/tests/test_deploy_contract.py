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
ALLOWED_HOSTS = {"ai-lab01.esxi", "ai-lab02.esxi"}
FORBIDDEN_PARTS = {".git", ".env", "config.json", ".venv", "node_modules", "__pycache__"}
EXTERNAL_COMMANDS = (
    "git",
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
    "cp",
    "mv",
    "rm",
    "mkdir",
    "install",
    "tee",
    "gzip",
    "sha256sum",
    "shasum",
    "find",
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
def command_stubs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Record external command names without ever recording environment data."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    call_log = tmp_path / "calls.log"
    for command in EXTERNAL_COMMANDS:
        _write_stub(bin_dir, command, call_log)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    return call_log, tmp_path


def _sandboxed_deploy_command(arguments: tuple[str, ...], sandbox_dir: Path | None) -> list[str]:
    """Build the only command shape allowed to execute the deferred CLI."""
    if sandbox_dir is None or not sandbox_dir.is_dir():
        pytest.skip("dynamic deploy CLI tests require an isolated writable sandbox directory")
    sandbox_exec = shutil.which("sandbox-exec")
    if not sandbox_exec:
        pytest.skip("dynamic deploy CLI tests require a network-denying, write-restricted OS sandbox")
    sandbox_profile = (
        "(version 1) "
        "(deny default) "
        "(allow file-read*) "
        "(allow process*) "
        f'(allow file-write* (subpath "{sandbox_dir}")) '
        "(deny network*)"
    )
    return [sandbox_exec, "-p", sandbox_profile, str(DEPLOY_SCRIPT), *arguments]


def _run_deploy(*arguments: str, sandbox_dir: Path | None = None) -> subprocess.CompletedProcess[str]:
    command = _sandboxed_deploy_command(arguments, sandbox_dir)
    _assert_deferred_cli_is_safe_to_execute()
    return subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
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


def _assert_deferred_cli_is_safe_to_execute(script: str | None = None) -> None:
    """Reject execution forms that can evade the isolated test harness."""
    if script is None:
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    unsafe_forms = re.findall(
        r"(?m)(?:^|[;&]\s*|\b(?:if|then|do|elif|else|while|until)\s+)"
        r"(?:source\s+|\.\s+|(?:export\s+)?PATH\s*=|(?:eval|exec|command|env|xargs|sh|bash|zsh|dash)\b|\$\{?[A-Za-z_])",
        script,
    )
    direct_executables = re.findall(
        r"(?m)(?:^|[;&|]\s*|\b(?:if|then|elif|do|while|until)\s+|\$\(\s*)"
        r"(?:[A-Za-z_][A-Za-z0-9_]*=[^\s]+\s+)*((?:/|\./|\.\./)[^\s;|&]+)",
        script,
    )
    assert not unsafe_forms and not direct_executables, (
        "deferred deploy CLI tests refuse source/PATH/indirection or direct executable paths "
        f"that evade the isolated harness: {unsafe_forms + direct_executables}"
    )


def _help_operations(help_text: str) -> set[str]:
    operation_lines = [line for line in help_text.splitlines() if line.startswith("Operations:")]
    assert len(operation_lines) == 1, "--help must contain one authoritative Operations declaration"
    advertised_operations = operation_lines[0].removeprefix("Operations:").split()
    assert set(advertised_operations) == EXPECTED_COMMANDS and len(advertised_operations) == len(
        EXPECTED_COMMANDS
    ), "Operations must advertise exactly the six release operations"
    usage_lines = [line for line in help_text.splitlines() if line.startswith("Usage:")]
    assert len(usage_lines) == 1 and re.fullmatch(
        r"Usage:\s+deploy\.sh\s+<operation>(?:\s+\[--host\s+HOST\])?", usage_lines[0]
    ), (
        "Usage must refer to the generic <operation> placeholder, not advertise a separate command"
    )
    return set(advertised_operations)


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


def test_help_exposes_only_the_release_operations(
    command_stubs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches an undocumented or missing deploy CLI operation."""
    _require_deploy_script()
    _, sandbox_dir = command_stubs

    def unexpected_subprocess(*_args: object, **_kwargs: object) -> None:
        pytest.fail("dynamic CLI execution must not proceed without OS isolation")

    with monkeypatch.context() as context:
        context.setattr(subprocess, "run", unexpected_subprocess)
        with pytest.raises(pytest.skip.Exception):
            _run_deploy("--help")
    with monkeypatch.context() as context:
        context.setattr(subprocess, "run", unexpected_subprocess)
        context.setattr(shutil, "which", lambda _name: None)
        with pytest.raises(pytest.skip.Exception):
            _run_deploy("--help", sandbox_dir=sandbox_dir)

    captured_commands: list[list[str]] = []

    def record_sandboxed_command(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured_commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    with monkeypatch.context() as context:
        context.setattr(subprocess, "run", record_sandboxed_command)
        _run_deploy("--help", sandbox_dir=sandbox_dir)
    assert captured_commands and captured_commands[0][0] == shutil.which("sandbox-exec")
    assert "(deny default)" in captured_commands[0][2]
    assert "(deny network*)" in captured_commands[0][2]
    assert "(allow file-write*" in captured_commands[0][2]

    result = _run_deploy("--help", sandbox_dir=sandbox_dir)
    assert result.returncode == 0, result.stderr
    assert _help_operations(result.stdout) == EXPECTED_COMMANDS
    assert _help_operations(
        "Usage: deploy.sh <operation> [--host HOST]\n"
        "Operations: rollback deploy status build verify preflight\n\n"
        "build creates an immutable local artifact.\n"
        "The descriptions may use arbitrary layout and wording.\n"
    ) == EXPECTED_COMMANDS
    with pytest.raises(AssertionError):
        _help_operations("Usage: deploy.sh destroy\nOperations: build preflight deploy verify status rollback\n")
    with pytest.raises(AssertionError):
        _help_operations(
            "Usage: deploy.sh <operation>\n"
            "Operations: build preflight deploy verify status rollback\n"
            "Operations: destroy\n"
        )


def test_unknown_host_exits_two_before_ssh(command_stubs: tuple[Path, Path]) -> None:
    """Catches host validation that occurs after a remote connection attempt."""
    _require_deploy_script()
    for bypass in (
        "source helper.sh\n",
        "PATH=/usr/bin\n",
        "eval \"ssh target\"\n",
        "bash helper.sh\n",
        "tool=/usr/bin/ssh\n$tool target\n",
        "$(/usr/bin/tar -cf release.tar release)\n",
    ):
        with pytest.raises(AssertionError):
            _assert_deferred_cli_is_safe_to_execute(bypass)
    command_log, sandbox_dir = command_stubs
    result = _run_deploy("preflight", "--host", "outside-ai-lab.esxi", sandbox_dir=sandbox_dir)
    assert result.returncode == 2
    assert not command_log.exists(), "unknown hosts must not invoke SSH, tar, or a build command"


def test_dirty_worktree_exits_three_before_build_or_transfer(
    tmp_path: Path, command_stubs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
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
    command_log, sandbox_dir = command_stubs
    result = _run_deploy("build", "--host", "ai-lab01.esxi", sandbox_dir=sandbox_dir)
    assert result.returncode == 3
    assert not command_log.exists(), "a dirty tree must stop before tar, SSH, or the build command"
