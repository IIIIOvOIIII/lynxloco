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
import yaml


DEPLOY_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = DEPLOY_DIR.parents[1]
DEPLOY_SCRIPT = DEPLOY_DIR / "deploy.sh"
ALLOWLIST = DEPLOY_DIR / "artifact-files.txt"
DOCKERFILE = DEPLOY_DIR / "Dockerfile"
COMPOSE_FILE = DEPLOY_DIR / "compose.yaml"
ENTRYPOINT = DEPLOY_DIR / "container-entrypoint.sh"
ACCEPTANCE_PYTEST = DEPLOY_DIR / "acceptance" / "pytest.ini"

RUNTIME_PLATFORM = "linux/amd64"
RUNTIME_BASE = (
    "--platform=linux/amd64 python:3.12-slim-bookworm@"
    "sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579"
)
EXPECTED_RUNTIME_ENV = {
    "MILOCO_HOME": "/var/lib/miloco",
    "MILOCO_DIRECTORIES__MODELS": "/opt/miloco/models",
    "MILOCO_SERVER__HOST": "0.0.0.0",
    "MILOCO_SERVER__PORT": "1810",
    "MILOCO_SERVER__URL": "http://127.0.0.1:1810",
}

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


def _docker_stages() -> dict[str, str]:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    stages: dict[str, str] = {}
    stage_name = ""
    stage_lines: list[str] = []
    for line in dockerfile.splitlines():
        from_match = re.match(
            r"FROM\s+(?:--platform=\S+\s+)?\S+(?:\s+AS\s+([A-Za-z0-9_-]+))?",
            line,
        )
        if from_match:
            if stage_name:
                stages[stage_name] = "\n".join(stage_lines)
            stage_name = from_match.group(1) or f"stage-{len(stages)}"
            stage_lines = [line]
            continue
        if stage_name:
            stage_lines.append(line)
    if stage_name:
        stages[stage_name] = "\n".join(stage_lines)
    return stages


def _docker_stage_effective_config() -> dict[str, dict[str, str]]:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    stage_config: dict[str, dict[str, str]] = {}
    current_stage = ""
    current_config: dict[str, str] = {}
    for line in dockerfile.splitlines():
        from_match = re.match(
            r"FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+([A-Za-z0-9_-]+))?",
            line,
        )
        if from_match:
            if current_stage:
                stage_config[current_stage] = current_config
            parent = from_match.group(1)
            current_stage = from_match.group(2) or f"stage-{len(stage_config)}"
            current_config = dict(stage_config.get(parent, {}))
            continue
        instruction_match = re.match(r"(ENTRYPOINT|CMD)\s+(.+)", line)
        if current_stage and instruction_match:
            current_config[instruction_match.group(1)] = instruction_match.group(2)
    if current_stage:
        stage_config[current_stage] = current_config
    return stage_config


def _compose_service() -> dict[str, object]:
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    assert set(compose["services"]) == {"miloco"}
    return compose["services"]["miloco"]


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
        pytest.fail(
            "dynamic deploy CLI contract cannot run safely without an isolated writable "
            "sandbox directory"
        )
    sandbox_exec = shutil.which("sandbox-exec")
    if sandbox_exec:
        sandbox_profile = (
            "(version 1) "
            "(deny default) "
            "(allow file-read*) "
            "(allow process*) "
            f'(allow file-write* (subpath "{sandbox_dir}")) '
            "(deny network*)"
        )
        return [sandbox_exec, "-p", sandbox_profile, str(DEPLOY_SCRIPT), *arguments]

    bubblewrap = shutil.which("bwrap")
    if bubblewrap:
        return [
            bubblewrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-net",
            "--ro-bind",
            "/",
            "/",
            "--ro-bind",
            str(REPOSITORY_ROOT),
            str(REPOSITORY_ROOT),
            "--bind",
            str(sandbox_dir),
            str(sandbox_dir),
            "--chdir",
            str(REPOSITORY_ROOT),
            str(DEPLOY_SCRIPT),
            *arguments,
        ]

    pytest.fail(
        "dynamic deploy CLI contract cannot run safely without an OS sandbox; "
        "install macOS sandbox-exec or Linux bubblewrap"
    )


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


def test_runtime_image_is_non_root_and_excludes_acceptance_payload() -> None:
    """Catches runtime images that run as root or carry test-only assets."""
    stages = _docker_stages()
    runtime = stages["runtime"]
    assert f"FROM {RUNTIME_BASE} AS runtime" in runtime
    assert "USER 10001:10001" in runtime
    assert "acceptance/" not in runtime
    assert "requirements/acceptance.txt" not in runtime
    assert re.search(r"pip\s+install\b[^\n]*--require-hashes", runtime)
    assert re.search(r"pip\s+install\b[^\n]*--no-deps\b[^\n]*wheels/miloco_miot-", runtime)
    assert re.search(r"pip\s+install\b[^\n]*--no-deps\b[^\n]*wheels/miloco-", runtime)
    assert re.search(r"pip\s+install\b[^\n]*--no-deps\b[^\n]*wheels/miloco_cli-", runtime)
    for name, value in EXPECTED_RUNTIME_ENV.items():
        assert f"{name}={value}" in runtime
    assert "urllib.request.urlopen('http://127.0.0.1:1810/health'" in runtime
    assert "container-entrypoint.sh" in runtime
    assert "ENTRYPOINT" in runtime


def test_acceptance_image_is_the_only_stage_with_acceptance_payload() -> None:
    """Catches acceptance dependencies leaking into the production runtime."""
    stages = _docker_stages()
    acceptance = stages["acceptance"]
    assert "FROM runtime AS acceptance" in acceptance
    assert "requirements/acceptance.txt" in acceptance
    assert "acceptance/" in acceptance
    assert re.search(r"pip\s+install\b[^\n]*--require-hashes", acceptance)
    assert "ai_lab_fixture" in acceptance
    assert ACCEPTANCE_PYTEST.read_text(encoding="utf-8").count("ai_lab_fixture") == 1


def test_acceptance_image_resets_runtime_entrypoint_before_fixture_command() -> None:
    """Catches inherited runtime entrypoints that wrap the acceptance pytest command."""
    effective_config = _docker_stage_effective_config()
    assert effective_config["runtime"]["ENTRYPOINT"] == '["container-entrypoint.sh"]'
    assert effective_config["acceptance"]["ENTRYPOINT"] == "[]"
    assert effective_config["acceptance"]["CMD"] == (
        '["python", "-m", "pytest", "-q", "-m", "ai_lab_fixture"]'
    )


def test_docker_and_compose_do_not_define_secrets() -> None:
    """Catches API key, token, or secret injection through image or Compose config."""
    deploy_config = DOCKERFILE.read_text(encoding="utf-8") + "\n" + COMPOSE_FILE.read_text(
        encoding="utf-8"
    )
    forbidden_names = (
        r"api[_-]?key|token|secret|password|credential|"
        r"rtsp[_-]?(?:url|uri|user(?:name)?|pass(?:word)?)|"
        r"(?:uri|url)[_-]?(?:user(?:name)?|pass(?:word)?)"
    )
    assert not re.search(rf"(?i)({forbidden_names})", deploy_config)
    assert "secrets:" not in deploy_config


def test_compose_runs_host_network_read_only_with_one_persistent_state_path() -> None:
    """Catches runtime Compose configs that open extra writable or persistent paths."""
    service = _compose_service()
    assert service["image"] == "miloco-lab:${MILOCO_RELEASE_SHA}"
    assert service["platform"] == RUNTIME_PLATFORM
    assert service["user"] == "10001:10001"
    assert service["network_mode"] == "host"
    assert service["restart"] == "unless-stopped"
    assert service["read_only"] is True
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["cap_drop"] == ["ALL"]
    assert service["environment"] == EXPECTED_RUNTIME_ENV
    assert service["volumes"] == ["/opt/miloco-lab/state:/var/lib/miloco"]
    assert service["tmpfs"] == ["/tmp:size=256m,mode=1777"]
    assert service["cpus"] == "${MILOCO_CPU_LIMIT}"
    assert service["mem_limit"] == "${MILOCO_MEMORY_LIMIT}"
    assert "build" not in service


def test_entrypoint_refuses_unsafe_state_directory_before_starting_backend() -> None:
    """Catches backends starting with unwritable or non-owner-only persistent state."""
    entrypoint = ENTRYPOINT.read_text(encoding="utf-8")
    assert "umask 077" in entrypoint
    assert "[ -w \"$state_dir\" ]" in entrypoint
    assert "stat -c '%u:%g'" in entrypoint
    assert "id -u" in entrypoint and "id -g" in entrypoint
    assert "stat -c '%a'" in entrypoint
    assert 'exec miloco-backend "$@"' in entrypoint
    assert not re.search(r"(?m)^\s*(?:env|printenv|set)\s*(?:$|[|;&>])", entrypoint)


def test_existing_deploy_script_without_os_sandbox_fails_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a green dynamic contract suite that never exercised deploy.sh."""
    deploy_script = tmp_path / "deploy.sh"
    deploy_script.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setitem(globals(), "DEPLOY_SCRIPT", deploy_script)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    subprocess_calls: list[list[str]] = []

    def record_subprocess(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        subprocess_calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", record_subprocess)
    signal = "none"
    try:
        _run_deploy("--help", sandbox_dir=tmp_path)
    except pytest.skip.Exception:
        signal = "skipped"
    except pytest.fail.Exception as error:
        assert "cannot run safely without an OS sandbox" in str(error)
        signal = "failed"
    assert subprocess_calls == []
    assert signal == "failed", "an existing deploy.sh without OS isolation must fail the suite"


def test_bubblewrap_sandbox_is_read_only_except_for_test_temp_and_denies_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a Linux fallback that exposes any writable path beyond test temp."""
    deploy_script = tmp_path / "deploy.sh"
    deploy_script.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setitem(globals(), "DEPLOY_SCRIPT", deploy_script)
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
    )

    try:
        command = _sandboxed_deploy_command(("--help",), tmp_path)
    except pytest.skip.Exception as error:
        pytest.fail(f"bubblewrap must be a supported dynamic contract sandbox: {error}")

    assert command == [
        "/usr/bin/bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-net",
        "--ro-bind",
        "/",
        "/",
        "--ro-bind",
        str(REPOSITORY_ROOT),
        str(REPOSITORY_ROOT),
        "--bind",
        str(tmp_path),
        str(tmp_path),
        "--chdir",
        str(REPOSITORY_ROOT),
        str(deploy_script),
        "--help",
    ]


def test_help_exposes_only_the_release_operations(
    command_stubs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches an undocumented or missing deploy CLI operation."""
    _require_deploy_script()
    _, sandbox_dir = command_stubs

    def unexpected_subprocess(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("dynamic CLI execution must not proceed without OS isolation")

    with monkeypatch.context() as context:
        context.setattr(subprocess, "run", unexpected_subprocess)
        with pytest.raises(pytest.fail.Exception, match="cannot run safely"):
            _run_deploy("--help")
    with monkeypatch.context() as context:
        context.setattr(subprocess, "run", unexpected_subprocess)
        context.setattr(shutil, "which", lambda _name: None)
        with pytest.raises(pytest.fail.Exception, match="cannot run safely without an OS sandbox"):
            _run_deploy("--help", sandbox_dir=sandbox_dir)

    captured_commands: list[list[str]] = []

    def record_sandboxed_command(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured_commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    with monkeypatch.context() as context:
        context.setattr(subprocess, "run", record_sandboxed_command)
        _run_deploy("--help", sandbox_dir=sandbox_dir)
    assert captured_commands
    sandbox_command = captured_commands[0]
    if Path(sandbox_command[0]).name == "sandbox-exec":
        assert "(deny default)" in sandbox_command[2]
        assert "(deny network*)" in sandbox_command[2]
        assert "(allow file-write*" in sandbox_command[2]
    else:
        assert Path(sandbox_command[0]).name == "bwrap"
        assert "--unshare-net" in sandbox_command
        assert ["--ro-bind", "/", "/"] == sandbox_command[
            sandbox_command.index("--ro-bind") :
        ][:3]
        assert ["--bind", str(sandbox_dir), str(sandbox_dir)] == sandbox_command[
            sandbox_command.index("--bind") :
        ][:3]

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
