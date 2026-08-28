"""Executable contract for the future ai-lab deployment CLI.

The deployment script is deliberately introduced by a later task.  These tests
activate their CLI assertions as soon as ``deploy.sh`` exists, while keeping
the allowlist contract independently enforceable from this first commit.
"""

from __future__ import annotations

import os
import hashlib
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml


DEPLOY_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = DEPLOY_DIR.parents[1]
DEPLOY_SCRIPT = REPOSITORY_ROOT / "deploy.sh"
REMOTE_RELEASE_SCRIPT = DEPLOY_DIR / "remote-release.sh"
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


def _write_release_receipt(repository: Path, sha: str, archive: Path) -> Path:
    controller = repository / "deploy" / "ai-lab" / "remote-release.sh"
    allowlist = repository / "deploy" / "ai-lab" / "artifact-files.txt"
    receipt = archive.with_name(f"miloco-lab-{sha}.receipt")
    receipt.write_text(
        "schema=1\n"
        f"git_sha={sha}\n"
        f"archive_sha256={hashlib.sha256(archive.read_bytes()).hexdigest()}\n"
        f"controller_sha256={hashlib.sha256(controller.read_bytes()).hexdigest()}\n"
        f"allowlist_sha256={hashlib.sha256(allowlist.read_bytes()).hexdigest()}\n"
        f"artifact_path={archive.relative_to(repository).as_posix()}\n",
        encoding="utf-8",
    )
    receipt.chmod(0o444)
    return receipt


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


def _assert_pre_guard_block_is_safe(script: str, guard_marker: str) -> None:
    """Reject execution indirection only in the pre-validation block.

    Approved build and remote paths necessarily occur after the host or clean
    worktree guard.  The executable sandbox tests below prove that failing
    guards exit before those paths can run.
    """
    assert script.count(guard_marker) == 1
    guarded_block = script.split(guard_marker, maxsplit=1)[0]
    unsafe_forms = re.findall(
        r"(?m)(?:^|[;&]\s*|\b(?:if|then|do|elif|else|while|until)\s+)"
        r"(?:source\s+|\.\s+|(?:export\s+)?PATH\s*=|(?:eval|exec|command|env|xargs|sh|bash|zsh|dash)\b|\$\{?[A-Za-z_])",
        guarded_block,
    )
    direct_executables = re.findall(
        r"(?m)(?:^|[;&|]\s*|\b(?:if|then|elif|do|while|until)\s+|\$\(\s*)"
        r"(?:[A-Za-z_][A-Za-z0-9_]*=[^\s]+\s+)*((?:/|\./|\.\./)[^\s;|&]+)",
        guarded_block,
    )
    assert not unsafe_forms and not direct_executables, (
        "deferred deploy CLI tests refuse source/PATH/indirection or direct executable paths "
        f"that evade the isolated harness: {unsafe_forms + direct_executables}"
    )


def _assert_deferred_cli_is_safe_to_execute(script: str | None = None) -> None:
    """Require explicit host/build guard boundaries without banning approved paths."""
    if script is None:
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    main = script.split("# DISPATCH_START", maxsplit=1)[1]
    host_branch = main.split("# HOST_GUARD_START", maxsplit=1)[1]
    _assert_pre_guard_block_is_safe(host_branch, "# HOST_VALIDATION_COMPLETE")
    build_branch = main.split("# BUILD_GUARD_START", maxsplit=1)[1]
    _assert_pre_guard_block_is_safe(build_branch, "# CLEAN_WORKTREE_VALIDATION_COMPLETE")


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
    result = _run_deploy("build", sandbox_dir=sandbox_dir)
    assert result.returncode == 3
    assert not command_log.exists(), "a dirty tree must stop before tar, SSH, or the build command"


@pytest.mark.parametrize("operation", ["preflight", "verify", "status", "rollback"])
def test_every_remote_operation_rejects_dirty_controller_before_ssh(
    operation: str,
    tmp_path: Path,
    command_stubs: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches read/status/rollback paths streaming a controller not equal to clean HEAD."""
    _require_deploy_script()
    command_log, sandbox_dir = command_stubs
    bin_dir = tmp_path / "dirty-controller-bin"
    bin_dir.mkdir()
    git_log = tmp_path / "dirty-controller-git.log"
    _write_stub(
        bin_dir,
        "git",
        git_log,
        "case \"$*\" in *status*) printf ' M deploy/ai-lab/remote-release.sh\\n' ;; esac",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    arguments = [operation, "ai-lab01.esxi"]
    if operation == "rollback":
        arguments.append("7" * 40)
    result = _run_deploy(*arguments, sandbox_dir=sandbox_dir)
    assert result.returncode == 3
    calls = command_log.read_text(encoding="utf-8") if command_log.exists() else ""
    assert "ssh" not in calls, "dirty controller proof must fail before every SSH path"


def test_remote_operation_rejects_untracked_controller_before_ssh(
    tmp_path: Path,
    command_stubs: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches an ignored or otherwise untracked remote controller bypassing exact-HEAD proof."""
    _require_deploy_script()
    command_log, sandbox_dir = command_stubs
    bin_dir = tmp_path / "untracked-controller-bin"
    bin_dir.mkdir()
    git_log = tmp_path / "untracked-controller-git.log"
    sha = "8" * 40
    _write_stub(
        bin_dir,
        "git",
        git_log,
        "case \"$*\" in "
        "*status*) exit 0 ;; "
        "*rev-parse*) printf '" + sha + "\\n' ;; "
        "*ls-files*) exit 1 ;; "
        "*diff*) exit 0 ;; "
        "esac",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    result = _run_deploy("status", "ai-lab01.esxi", sandbox_dir=sandbox_dir)
    assert result.returncode == 3
    calls = command_log.read_text(encoding="utf-8") if command_log.exists() else ""
    assert "ssh" not in calls, "untracked controller must fail before SSH"


def test_guard_scope_allows_only_post_validation_approved_paths() -> None:
    """Catches both pre-guard indirection and a guard that bans required build paths."""
    marker = "# VALIDATION_COMPLETE"
    for bypass in (
        "source helper.sh\n",
        "PATH=/usr/bin\n",
        "eval \"ssh target\"\n",
        "bash helper.sh\n",
        "tool=/usr/bin/ssh\n$tool target\n",
        "$(/usr/bin/tar -cf release.tar release)\n",
    ):
        with pytest.raises(AssertionError):
            _assert_pre_guard_block_is_safe(f"{bypass}{marker}\n", marker)
    _assert_pre_guard_block_is_safe(
        f"validate_host \"$host\"\n{marker}\n./scripts/build.sh\n",
        marker,
    )


def test_controller_and_readme_use_the_canonical_root_interface() -> None:
    """Catches reintroduction of the deprecated deploy/ai-lab controller path."""
    _require_deploy_script()
    readme = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")
    assert "./deploy.sh build" in readme
    assert "./deploy.sh preflight ai-lab01.esxi" in readme
    assert "./deploy/ai-lab/deploy.sh" not in readme
    assert "/opt/miloco-lab/state" in readme
    assert "/opt/miloco-lab/deploy-state/current" in readme
    assert "/opt/miloco-lab/deploy-state/previous" in readme
    assert "/var/lib/miloco-ai-lab" not in readme


def test_build_contract_is_content_addressed_locked_and_bounded() -> None:
    """Catches non-reproducible dependency export or broad source staging."""
    _require_deploy_script()
    controller = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "./scripts/build.sh --packages web,miloco-miot,miloco,miloco-cli" in controller
    assert controller.count("uv export --locked --no-dev --no-emit-workspace") >= 2
    assert "uv export --locked --no-emit-workspace" in controller
    for path in (
        "backend/miloco/tests/integration/test_rtsp_perception.py",
        "backend/miloco/tests/integration/test_rtsp_live_view.py",
        "backend/miloco/tests/integration/test_responses_perception.py",
        "backend/miloco/tests/integration/responses_fixture_server.py",
        "backend/miloco/tests/fixtures/rtsp",
        "scripts/rtsp-smoke.sh",
        "scripts/rtsp-view-smoke.sh",
        "scripts/responses-vlm-smoke.sh",
    ):
        assert path in controller
    assert "mktemp -d" in controller
    assert "artifact-files.txt" in controller
    assert 'find "$staging" -mindepth 1 -print0' in controller
    assert '[[ ! -L "$path" ]]' in controller
    assert "release.json" in controller
    assert "SHA256SUMS" in controller
    assert "miloco-lab-${sha}.tar.gz" in controller
    assert "miloco-lab-${sha}.receipt" in controller
    for receipt_field in (
        "schema=1",
        "git_sha=",
        "archive_sha256=",
        "controller_sha256=",
        "allowlist_sha256=",
        "artifact_path=",
    ):
        assert receipt_field in controller
    assert "read_release_receipt" in controller
    assert "tar -czf -" not in controller
    assert not re.search(r"tar\b[^\n]*-C\s+\"?\$?PROJECT_ROOT", controller)
    for credential_name in (
        "MILOCO_MODEL__OMNI__API_KEY",
        "MILOCO_RESPONSES_API_KEY",
        "MILOCO_RTSP_TEST_URL",
        "MILOCO_RTSP_TEST_USERNAME",
        "MILOCO_RTSP_TEST_PASSWORD",
    ):
        assert f"-u {credential_name}" in controller


def test_preflight_and_transfer_are_bounded_to_the_two_labs() -> None:
    """Catches weak host/platform checks or repository-wide transfer methods."""
    _require_deploy_script()
    controller = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    remote = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8")
    assert set(re.findall(r"ai-lab0[12]\.esxi", controller)) == ALLOWED_HOSTS
    assert "Docker >= 26" in remote
    assert "Compose >= 2.26" in remote
    assert "linux/amd64" in remote
    assert "1810" in remote
    assert str(5 * 1024 * 1024) in remote
    assert 'LAB_ROOT="/opt/miloco-lab"' in remote
    assert re.search(r"\[\[?\s+!?\s*-L\s+\"\$LAB_ROOT\"", remote)
    assert 'docker_command 10 top "$container_id" -eo pid' in remote
    assert "listener_pid" in remote
    assert 'REMOTE_CONTROL_DIR="${REMOTE_ROOT}/control"' in controller
    assert "controller_digest" in controller
    assert "${REMOTE_CONTROL_DIR}/${controller_digest}/remote-release.sh" in controller
    assert "sha256_file" in controller
    assert "transaction" in controller
    assert "remote-release.sh" in controller
    assert not re.search(r"\b(?:scp|sftp|rclone|oras|skopeo)\b", controller)
    assert not re.search(r"rsync\b[^\n]*\s\.\s", controller)
    assert not re.search(r"tar\b[^\n]*-C\s+\"?\$?PROJECT_ROOT", controller)


def test_remote_checksum_and_acceptance_precede_activation() -> None:
    """Catches building or switching from an unverified/unaccepted release."""
    remote = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8")
    verify_body = remote.split("verify_release_tree()", maxsplit=1)[1].split("\n}", maxsplit=1)[0]
    locked_body = remote.split("build_and_activate_locked()", maxsplit=1)[1].split(
        "\n}", maxsplit=1
    )[0]
    build_body = remote.split("build_images_and_accept()", maxsplit=1)[1].split(
        "\n}", maxsplit=1
    )[0]
    assert 'sha256sum -c "SHA256SUMS"' in verify_body
    assert "verify_archive_digest" in remote
    assert "validate_archive_members" in remote
    assert remote.index("verify_archive_digest") < remote.index("validate_archive_members")
    assert remote.index("validate_archive_members") < remote.index("--extract")
    for forbidden_type in ("fifo", "device", "socket", "hardlink", "symlink"):
        assert forbidden_type in remote
    assert build_body.index("invalidate_acceptance") < build_body.index("--target runtime")
    assert build_body.index("--target runtime") < build_body.index("--target acceptance")
    assert "miloco-lab-acceptance-candidate:$sha" in build_body
    assert build_body.index("--target acceptance") < build_body.index(
        'run --rm --network none "$candidate_acceptance"'
    )
    assert build_body.index("miloco-lab-acceptance:$sha") < build_body.index("mark_acceptance_success")
    assert "runtime_image_id" in build_body
    assert "acceptance_image_id" in build_body
    assert build_body.index("miloco-lab-acceptance:$sha") < build_body.index(
        "mark_acceptance_success"
    )
    assert locked_body.index("verify_release") < locked_body.index("build_images_and_accept")
    assert locked_body.index("build_images_and_accept") < locked_body.index("activate_release")
    assert "--platform linux/amd64" in remote
    assert 'install -d -o 10001 -g 10001 -m 0700 "$STATE_DIR"' in remote
    assert 'ai-lab01.esxi) cpu_limit="3.0"; memory_limit="3072m"' in remote
    assert 'ai-lab02.esxi) cpu_limit="1.25"; memory_limit="1536m"' in remote


def test_activation_and_health_failure_preserve_rollback_state() -> None:
    """Catches current-before-previous writes and failed health without restoration."""
    remote = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8")
    activate_body = remote.split("activate_release()", maxsplit=1)[1].split("\n}", maxsplit=1)[0]
    previous_write = activate_body.index('atomic_write "$PREVIOUS_FILE"')
    current_write = activate_body.index('atomic_write "$CURRENT_FILE"')
    assert previous_write < activate_body.index("compose_up") < current_write
    assert "HEALTH_TIMEOUT_SECONDS=120" in remote
    assert "State.Health.Status" in remote
    assert "http://127.0.0.1:1810/health" in remote
    assert "restore_previous" in remote
    assert 'TRANSITION_LOCK_FILE="$DEPLOY_STATE_DIR/transition.lock"' in remote
    assert "flock -n" in remote
    assert "arm_transition" in activate_body
    assert "transition_exit" in remote
    assert "ROLLBACK_FAILED_EXIT_CODE=70" in remote
    assert "trap 'exit 129' HUP" in remote
    assert "trap 'exit 130' INT" in remote
    assert "trap 'exit 143' TERM" in remote
    assert "restore_previous" in remote
    assert "remove_candidate" in remote
    assert "rollback_failed" in remote
    assert "restore_previous" not in remote.split("|| true")[-1]


def test_explicit_rollback_requires_verified_release_and_image_without_state_delete() -> None:
    """Catches rollback to an unknown SHA or destructive state cleanup."""
    remote = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8")
    rollback = remote.split("rollback_release()", maxsplit=1)[1]
    assert "release_capability" in rollback
    assert "activate_release" in rollback
    assert not re.search(r"rm\s+(?:-[A-Za-z]*r[A-Za-z]*f?|--recursive)[^\n]*\$STATE_DIR", remote)
    assert not re.search(r"rm\s+[^\n]*\$CURRENT_FILE|rm\s+[^\n]*\$PREVIOUS_FILE", remote)


def test_status_path_is_read_only_and_does_not_create_state() -> None:
    """Catches status implementations that mutate an undeployed host."""
    remote = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8")
    status_body = remote.split("status_release()", maxsplit=1)[1].split("\n}", maxsplit=1)[0]
    assert "not_deployed" in status_body
    assert not re.search(r"\b(?:mkdir|install|touch|mv|rm|docker compose .* up)\b", status_body)


def test_success_retains_current_and_two_prior_releases_and_images() -> None:
    """Catches unbounded release growth or cleanup that removes rollback capacity."""
    remote = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8")
    assert "retain_rollback_history" in remote
    retention_body = remote.split("retain_rollback_history()", maxsplit=1)[1].split(
        "\n}", maxsplit=1
    )[0]
    assert "historical_kept=0" in retention_body
    assert "historical_kept < 2" in retention_body
    assert "PREVIOUS_FILE" in retention_body
    assert "release_capability" in retention_body
    assert "remove_release_pair" in retention_body
    assert "probe_error" in retention_body
    activate_body = remote.split("activate_release()", maxsplit=1)[1].split("\n}", maxsplit=1)[0]
    assert activate_body.index('atomic_write "$CURRENT_FILE"') < activate_body.index(
        "retain_rollback_history"
    )
    assert "activated_cleanup_failed" in activate_body


def test_deploy_streams_one_archive_through_stubbed_ssh(tmp_path: Path) -> None:
    """Catches transfer drift to broad copies or non-content-addressed remote paths."""
    sha = "0123456789abcdef0123456789abcdef01234567"
    repository = tmp_path / "repo"
    controller = repository / "deploy.sh"
    remote_dir = repository / "deploy" / "ai-lab"
    remote_dir.mkdir(parents=True)
    shutil.copy2(DEPLOY_SCRIPT, controller)
    shutil.copy2(REMOTE_RELEASE_SCRIPT, remote_dir / "remote-release.sh")
    shutil.copy2(ALLOWLIST, remote_dir / "artifact-files.txt")
    archive = repository / "dist" / "lab" / sha / f"miloco-lab-{sha}.tar.gz"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"stubbed-release-archive")
    archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    controller_digest = hashlib.sha256(REMOTE_RELEASE_SCRIPT.read_bytes()).hexdigest()
    allowlist_digest = hashlib.sha256(ALLOWLIST.read_bytes()).hexdigest()
    _write_release_receipt(repository, sha, archive)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_stub = bin_dir / "git"
    git_stub.write_text(
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        "  *status*) exit 0 ;;\n"
        "  *ls-files*) exit 0 ;;\n"
        "  *diff*) exit 0 ;;\n"
        f"  *rev-parse*) printf '%s\\n' '{sha}' ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    git_stub.chmod(0o755)
    ssh_stub = bin_dir / "ssh"
    ssh_stub.write_text(
        "#!/usr/bin/env bash\n"
        f"counter='{tmp_path / 'ssh-count'}'\n"
        "count=0\n"
        "if [[ -f \"$counter\" ]]; then read -r count < \"$counter\"; fi\n"
        "count=$((count + 1))\n"
        "printf '%s\\n' \"$count\" > \"$counter\"\n"
        f"printf '%s\\n' \"$*\" > '{tmp_path}/ssh-args-'\"$count\"\n"
        f"/bin/cat > '{tmp_path}/ssh-stdin-'\"$count\"\n",
        encoding="utf-8",
    )
    ssh_stub.chmod(0o755)
    result = subprocess.run(
        [str(controller), "deploy", "ai-lab01.esxi"],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "ssh-count").read_text(encoding="utf-8").strip() == "3"
    install_args = (tmp_path / "ssh-args-1").read_text(encoding="utf-8")
    assert controller_digest in install_args
    assert f"/opt/miloco-lab/control/{controller_digest}/remote-release.sh" in install_args
    assert (tmp_path / "ssh-stdin-1").read_bytes() == REMOTE_RELEASE_SCRIPT.read_bytes()
    preflight_args = (tmp_path / "ssh-args-2").read_text(encoding="utf-8")
    assert f"/opt/miloco-lab/control/{controller_digest}/remote-release.sh" in preflight_args
    assert "preflight ai-lab01.esxi" in preflight_args
    assert (tmp_path / "ssh-stdin-2").read_bytes() == b""
    transaction_args = (tmp_path / "ssh-args-3").read_text(encoding="utf-8")
    assert f"/opt/miloco-lab/control/{controller_digest}/remote-release.sh" in transaction_args
    assert (
        f"transaction ai-lab01.esxi {sha} {archive_digest} {controller_digest} "
        f"{allowlist_digest}"
    ) in transaction_args
    assert (tmp_path / "ssh-stdin-3").read_bytes() == archive.read_bytes()


@pytest.mark.parametrize("replacement_target", ["archive", "controller"])
def test_deploy_rejects_replacement_after_clean_build_receipt(
    tmp_path: Path, replacement_target: str
) -> None:
    """Catches deploy endorsing replaced archive or controller bytes after clean build."""
    sha = "1" * 40
    repository = tmp_path / "receipt-repo"
    remote_dir = repository / "deploy" / "ai-lab"
    remote_dir.mkdir(parents=True)
    controller = repository / "deploy.sh"
    shutil.copy2(DEPLOY_SCRIPT, controller)
    shutil.copy2(REMOTE_RELEASE_SCRIPT, remote_dir / "remote-release.sh")
    shutil.copy2(ALLOWLIST, remote_dir / "artifact-files.txt")
    archive = repository / "dist" / "lab" / sha / f"miloco-lab-{sha}.tar.gz"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"receipt-bound-archive")
    _write_release_receipt(repository, sha, archive)
    if replacement_target == "archive":
        archive.write_bytes(b"replacement-after-build")
    else:
        (remote_dir / "remote-release.sh").write_bytes(b"replacement-controller-after-build")

    bin_dir = tmp_path / "receipt-bin"
    bin_dir.mkdir()
    git_stub = bin_dir / "git"
    git_stub.write_text(
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        "  *status*|*ls-files*|*diff*) exit 0 ;;\n"
        f"  *rev-parse*) printf '%s\\n' '{sha}' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    git_stub.chmod(0o755)
    ssh_log = tmp_path / "receipt-ssh.log"
    _write_stub(bin_dir, "ssh", ssh_log)
    result = subprocess.run(
        [str(controller), "deploy", "ai-lab01.esxi"],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode == 4
    assert "receipt" in result.stderr
    assert not ssh_log.exists(), "receipt mismatch must fail before preflight SSH"


def test_transaction_rehashes_exact_controller_inside_one_lock(tmp_path: Path) -> None:
    """Catches split receive/build locks or a digest-addressed controller swap."""
    sha = "2" * 40
    digest = "3" * 64
    source = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8").rsplit(
        '\nmain "$@"', maxsplit=1
    )[0]
    call_log = tmp_path / "transaction.log"
    harness = tmp_path / "transaction-harness.sh"
    harness.write_text(
        source
        + "\nrequire_root() { :; }\n"
        + f"acquire_transition_lock() {{ lock_held=1; printf 'lock\\n' >> '{call_log}'; }}\n"
        + "verify_controller_self() { [[ \"${lock_held:-0}\" == 1 ]]; "
        + f"printf 'controller:%s\\n' \"$1\" >> '{call_log}'; }}\n"
        + "receive_release_locked() { [[ \"${lock_held:-0}\" == 1 ]]; "
        + f"printf 'receive\\n' >> '{call_log}'; /bin/cat >/dev/null; }}\n"
        + "build_and_activate_locked() { [[ \"${lock_held:-0}\" == 1 ]]; "
        + f"printf 'build-activate\\n' >> '{call_log}'; }}\n"
        + f'transaction_release "ai-lab01.esxi" "{sha}" "{digest}" "{digest}" "{digest}"\n',
        encoding="utf-8",
    )
    harness.chmod(0o755)
    result = subprocess.run(
        [str(harness)],
        input="archive",
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert call_log.read_text(encoding="utf-8").splitlines() == [
        "lock",
        f"controller:{digest}",
        "receive",
        "build-activate",
    ]


def test_remote_allowlist_rejects_extra_regular_files() -> None:
    """Catches a checksum-complete but locally non-allowlisted regular file on the host."""
    source = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8").rsplit(
        '\nmain "$@"', maxsplit=1
    )[0]
    command = (
        source
        + "\nremote_path_is_allowlisted Dockerfile\n"
        + "remote_path_is_allowlisted acceptance/integration/test_rtsp_perception.py\n"
        + "if remote_path_is_allowlisted extra-regular.txt; then exit 91; fi\n"
    )
    result = subprocess.run(
        ["/bin/bash"], input=command, text=True, capture_output=True, check=False, timeout=5
    )
    assert result.returncode == 0, result.stderr


def test_all_compose_calls_use_one_resource_and_timeout_wrapper(tmp_path: Path) -> None:
    """Catches Compose reads/writes that silently lose the selected host resource profile."""
    remote = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8")
    assert remote.count("docker compose") == 1
    wrapper = remote.split("compose_command()", maxsplit=1)[1].split("\n}", maxsplit=1)[0]
    for name in ("MILOCO_RELEASE_SHA", "MILOCO_CPU_LIMIT", "MILOCO_MEMORY_LIMIT"):
        assert name in wrapper
    assert "timeout" in wrapper

    source = remote.rsplit('\nmain "$@"', maxsplit=1)[0]
    harness = tmp_path / "compose-wrapper.sh"
    harness.write_text(
        source + '\ncompose_command "ai-lab02.esxi" "' + "3" * 40 + '" 7 ps -q miloco\n',
        encoding="utf-8",
    )
    harness.chmod(0o755)
    bin_dir = tmp_path / "compose-bin"
    bin_dir.mkdir()
    wrapper_log = tmp_path / "wrapper.log"
    timeout_stub = bin_dir / "timeout"
    timeout_stub.write_text(
        "#!/usr/bin/env bash\n"
        f"printf 'sha=%s cpu=%s memory=%s args=%s\\n' \"$MILOCO_RELEASE_SHA\" \"$MILOCO_CPU_LIMIT\" \"$MILOCO_MEMORY_LIMIT\" \"$*\" > '{wrapper_log}'\n",
        encoding="utf-8",
    )
    timeout_stub.chmod(0o755)
    result = subprocess.run(
        [str(harness)],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode == 0, result.stderr
    assert wrapper_log.read_text(encoding="utf-8").startswith(
        f"sha={'3' * 40} cpu=1.25 memory=1536m"
    )


def test_rollback_requires_artifact_bound_acceptance_marker_before_images(tmp_path: Path) -> None:
    """Catches an old marker approving rebuilt image tags with different immutable IDs."""
    sha = "4" * 40
    digest = "5" * 64
    controller_digest = "6" * 64
    allowlist_digest = hashlib.sha256(ALLOWLIST.read_bytes()).hexdigest()
    runtime_id = "sha256:" + "7" * 64
    acceptance_id = "sha256:" + "8" * 64
    lab_root = tmp_path / "miloco-lab"
    artifact_dir = lab_root / "deploy-state" / "artifacts"
    accepted_dir = lab_root / "deploy-state" / "accepted"
    artifact_dir.mkdir(parents=True)
    accepted_dir.mkdir(parents=True)
    (artifact_dir / sha).write_text(
        "schema=1\n"
        f"git_sha={sha}\n"
        f"archive_sha256={digest}\n"
        f"controller_sha256={controller_digest}\n"
        f"allowlist_sha256={allowlist_digest}\n",
        encoding="utf-8",
    )
    source = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8")
    source = source.rsplit('\nmain "$@"', maxsplit=1)[0]
    source = source.replace('readonly LAB_ROOT="/opt/miloco-lab"', f'readonly LAB_ROOT="{lab_root}"')
    harness = tmp_path / "rollback-capable.sh"
    harness.write_text(
        source
        + "\nverify_release() { return 0; }\n"
        + "release_contract_state() { printf 'valid\\n'; }\n"
        + "classifier_capability_paths_state() { return 0; }\n"
        + "require_safe_directory() { return 0; }\n"
        + "require_safe_record() { return 0; }\n"
        + "safe_directory_state() { [[ -d \"$1\" && ! -L \"$1\" ]] || return 1; return 0; }\n"
        + "safe_record_state() { [[ -f \"$1\" && ! -L \"$1\" ]] || return 1; return 0; }\n"
        + f"image_reference_state() {{ case \"$1\" in miloco-lab:*) printf 'present:%s\\n' '{runtime_id}' ;; *) printf 'present:%s\\n' '{acceptance_id}' ;; esac; }}\n"
        + 'release_capability "$1"\n',
        encoding="utf-8",
    )
    harness.chmod(0o755)
    missing = subprocess.run(
        [str(harness), sha], text=True, capture_output=True, check=False, timeout=5
    )
    assert missing.returncode == 0, missing.stderr
    assert missing.stdout.strip() == "definitively_invalid"

    (accepted_dir / sha).write_text(
        "schema=1\n"
        f"archive_sha256={digest}\n"
        f"runtime_image_id={runtime_id}\n"
        f"acceptance_image_id={acceptance_id}\n",
        encoding="utf-8",
    )
    accepted = subprocess.run(
        [str(harness), sha], text=True, capture_output=True, check=False, timeout=5
    )
    assert accepted.returncode == 0, accepted.stderr
    assert accepted.stdout.strip() == "capable"

    mismatched_source = harness.read_text(encoding="utf-8").replace(runtime_id, "sha256:" + "9" * 64)
    mismatch_harness = tmp_path / "rollback-mismatch.sh"
    mismatch_harness.write_text(mismatched_source, encoding="utf-8")
    mismatch_harness.chmod(0o755)
    mismatch = subprocess.run(
        [str(mismatch_harness), sha], text=True, capture_output=True, check=False, timeout=5
    )
    assert mismatch.returncode == 0, mismatch.stderr
    assert mismatch.stdout.strip() == "definitively_invalid"


@pytest.mark.parametrize(
    ("probe_case", "expected_state"),
    [
        ("missing_image", "definitively_invalid"),
        ("corrupt_release", "definitively_invalid"),
        ("marker_mismatch", "definitively_invalid"),
        ("daemon_error", "probe_error"),
    ],
)
def test_release_capability_distinguishes_invalid_debris_from_probe_uncertainty(
    tmp_path: Path, probe_case: str, expected_state: str
) -> None:
    """Exercises the real classifier instead of replacing it in retention tests."""
    sha = "9" * 40
    archive_digest = "a" * 64
    runtime_id = "sha256:" + "b" * 64
    acceptance_id = "sha256:" + "c" * 64
    lab_root = tmp_path / "miloco-lab"
    release = lab_root / "releases" / sha
    artifacts = lab_root / "deploy-state" / "artifacts"
    accepted = lab_root / "deploy-state" / "accepted"
    release.mkdir(parents=True)
    artifacts.mkdir(parents=True)
    accepted.mkdir(parents=True)
    (artifacts / sha).write_text(
        "schema=1\n"
        f"git_sha={sha}\n"
        f"archive_sha256={archive_digest}\n"
        f"controller_sha256={'d' * 64}\n"
        f"allowlist_sha256={hashlib.sha256(ALLOWLIST.read_bytes()).hexdigest()}\n",
        encoding="utf-8",
    )
    marker_runtime_id = "sha256:" + "e" * 64 if probe_case == "marker_mismatch" else runtime_id
    (accepted / sha).write_text(
        "schema=1\n"
        f"archive_sha256={archive_digest}\n"
        f"runtime_image_id={marker_runtime_id}\n"
        f"acceptance_image_id={acceptance_id}\n",
        encoding="utf-8",
    )
    source = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8").rsplit(
        '\nmain "$@"', maxsplit=1
    )[0]
    source = source.replace('readonly LAB_ROOT="/opt/miloco-lab"', f'readonly LAB_ROOT="{lab_root}"')
    harness = tmp_path / f"classifier-{probe_case}.sh"
    verify_override = (
        "\nrelease_contract_state() { printf 'definitively_invalid\\n'; }\n"
        if probe_case == "corrupt_release"
        else "\nverify_release() { return 0; }\nrelease_contract_state() { printf 'valid\\n'; }\n"
    )
    harness.write_text(
        source
        + "\nrequire_safe_directory() { return 0; }\n"
        + "classifier_capability_paths_state() { return 0; }\n"
        + "require_safe_record() { return 0; }\n"
        + "safe_directory_state() { [[ -d \"$1\" && ! -L \"$1\" ]] || return 1; return 0; }\n"
        + "safe_record_state() { [[ -f \"$1\" && ! -L \"$1\" ]] || return 1; return 0; }\n"
        + verify_override
        + "docker_command() {\n"
        + f"  case '{probe_case}:$*' in\n"
        + "    missing_image:*' image ls '*) return 0 ;;\n"
        + "    missing_image:*' image inspect '*) return 1 ;;\n"
        + "    daemon_error:*) return 75 ;;\n"
        + "    corrupt_release:*) return 88 ;;\n"
        + "    marker_mismatch:*' image ls '*) printf 'listed\\n' ;;\n"
        + f"    marker_mismatch:*miloco-lab-acceptance:*) printf '%s\\n' '{acceptance_id}' ;;\n"
        + f"    marker_mismatch:*) printf '%s\\n' '{runtime_id}' ;;\n"
        + "  esac\n"
        + "}\n"
        + f'release_capability "{sha}"\n',
        encoding="utf-8",
    )
    harness.chmod(0o755)
    result = subprocess.run(
        [str(harness)], text=True, capture_output=True, check=False, timeout=5
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected_state


def test_failed_acceptance_invalidates_old_marker_and_cleans_rebuilt_tags(tmp_path: Path) -> None:
    """Catches old acceptance proof surviving a same-SHA rebuild that later fails tests."""
    sha = "a" * 40
    lab_root = tmp_path / "miloco-lab"
    accepted_dir = lab_root / "deploy-state" / "accepted"
    accepted_dir.mkdir(parents=True)
    marker = accepted_dir / sha
    marker.write_text("old-marker\n", encoding="utf-8")
    source = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8").rsplit(
        '\nmain "$@"', maxsplit=1
    )[0]
    source = source.replace('readonly LAB_ROOT="/opt/miloco-lab"', f'readonly LAB_ROOT="{lab_root}"')
    call_log = tmp_path / "rebuild.log"
    harness = tmp_path / "failed-acceptance.sh"
    harness.write_text(
        source
        + "\nrequire_safe_directory() { return 0; }\n"
        + "release_capability() { printf 'definitively_invalid\\n'; }\n"
        + "require_safe_record() { return 0; }\n"
        + "safe_directory_state() { [[ -d \"$1\" && ! -L \"$1\" ]] || return 1; return 0; }\n"
        + "safe_record_state() { [[ -f \"$1\" && ! -L \"$1\" ]] || return 1; return 0; }\n"
        + f"remove_image_tags() {{ printf 'remove-tags\\n' >> '{call_log}'; return 0; }}\n"
        + "docker_command() {\n"
        + f"  printf 'docker:%s\\n' \"$*\" >> '{call_log}'\n"
        + "  case \"$*\" in *' run '*) return 17 ;; *) return 0 ;; esac\n"
        + "}\n"
        + f'build_images_and_accept "ai-lab01.esxi" "{sha}" "{lab_root}/releases/{sha}"\n',
        encoding="utf-8",
    )
    harness.chmod(0o755)
    result = subprocess.run(
        [str(harness)], text=True, capture_output=True, check=False, timeout=5
    )
    assert result.returncode != 0
    assert not marker.exists(), "same-SHA rebuild must atomically invalidate old acceptance"
    calls = call_log.read_text(encoding="utf-8")
    assert calls.count("remove-tags") == 1, (
        "canonical tags must remain untouched until isolated-candidate acceptance fails"
    )
    assert calls.count("image rm miloco-lab-candidate:") == 2, (
        "candidate tags must be cleared before build and again after failed acceptance"
    )


@pytest.mark.parametrize("protected_record", ["current", "previous"])
def test_same_sha_retry_reuses_protected_accepted_images_without_mutation(
    tmp_path: Path, protected_record: str
) -> None:
    """Catches a retry destroying the current or previous SHA's known-good proof."""
    sha = "1" * 40
    lab_root = tmp_path / "miloco-lab"
    deploy_state = lab_root / "deploy-state"
    accepted = deploy_state / "accepted"
    accepted.mkdir(parents=True)
    (deploy_state / protected_record).write_text(f"{sha}\n", encoding="utf-8")
    marker = accepted / sha
    marker.write_text("known-good-proof\n", encoding="utf-8")
    mutation_log = tmp_path / f"protected-{protected_record}.log"
    source = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8").rsplit(
        '\nmain "$@"', maxsplit=1
    )[0]
    source = source.replace('readonly LAB_ROOT="/opt/miloco-lab"', f'readonly LAB_ROOT="{lab_root}"')
    harness = tmp_path / f"protected-{protected_record}.sh"
    harness.write_text(
        source
        + "\nrequire_safe_directory() { return 0; }\n"
        + "require_safe_record() { return 0; }\n"
        + "safe_directory_state() { [[ -d \"$1\" && ! -L \"$1\" ]] || return 1; return 0; }\n"
        + "safe_record_state() { [[ -f \"$1\" && ! -L \"$1\" ]] || return 1; return 0; }\n"
        + "release_capability() { printf 'capable\\n'; }\n"
        + f"docker_command() {{ printf 'docker:%s\\n' \"$*\" >> '{mutation_log}'; return 0; }}\n"
        + f"remove_image_tags() {{ printf 'remove-canonical\\n' >> '{mutation_log}'; return 0; }}\n"
        + f"mark_acceptance_success() {{ printf 'mark\\n' >> '{mutation_log}'; return 0; }}\n"
        + f'build_images_and_accept "ai-lab01.esxi" "{sha}" "{lab_root}/releases/{sha}"\n',
        encoding="utf-8",
    )
    harness.chmod(0o755)
    result = subprocess.run(
        [str(harness)], text=True, capture_output=True, check=False, timeout=5
    )
    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="utf-8") == "known-good-proof\n"
    assert not mutation_log.exists(), "protected accepted retry must be a mutation-free reuse"


@pytest.mark.parametrize(
    ("capability", "expected_returncode", "expects_mutation"),
    [
        ("capable", 0, False),
        ("probe_error", 1, False),
        ("definitively_invalid", 1, True),
    ],
)
def test_same_sha_retry_classifies_non_pointer_history_before_any_mutation(
    tmp_path: Path,
    capability: str,
    expected_returncode: int,
    expects_mutation: bool,
) -> None:
    """All historical pairs are classified before a same-SHA candidate is touched."""
    sha = "6" * 40
    lab_root = tmp_path / "miloco-lab"
    accepted = lab_root / "deploy-state" / "accepted"
    accepted.mkdir(parents=True)
    marker = accepted / sha
    marker.write_text("historical-proof\n", encoding="utf-8")
    mutation_log = tmp_path / f"historical-{capability}.log"
    source = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8").rsplit(
        '\nmain "$@"', maxsplit=1
    )[0]
    source = source.replace('readonly LAB_ROOT="/opt/miloco-lab"', f'readonly LAB_ROOT="{lab_root}"')
    harness = tmp_path / f"historical-{capability}.sh"
    harness.write_text(
        source
        + "\nrequire_safe_directory() { return 0; }\n"
        + "require_safe_record() { return 0; }\n"
        + "safe_directory_state() { [[ -d \"$1\" && ! -L \"$1\" ]] || return 1; return 0; }\n"
        + "safe_record_state() { [[ -f \"$1\" && ! -L \"$1\" ]] || return 1; return 0; }\n"
        + f"release_capability() {{ printf '{capability}\\n'; }}\n"
        + f"invalidate_acceptance() {{ printf 'marker\\n' >> '{mutation_log}'; return 0; }}\n"
        + f"remove_candidate_image_tags() {{ printf 'candidate-tags\\n' >> '{mutation_log}'; return 0; }}\n"
        + f"remove_image_tags() {{ printf 'canonical-tags\\n' >> '{mutation_log}'; return 0; }}\n"
        + "docker_command() { return 17; }\n"
        + f'build_images_and_accept "ai-lab01.esxi" "{sha}" "{lab_root}/releases/{sha}"\n',
        encoding="utf-8",
    )
    harness.chmod(0o755)
    result = subprocess.run(
        [str(harness)], text=True, capture_output=True, check=False, timeout=5
    )
    assert result.returncode == expected_returncode, result.stderr
    if expects_mutation:
        assert mutation_log.exists(), "only confirmed invalid history may enter candidate rebuild"
        assert "marker" in mutation_log.read_text(encoding="utf-8")
    else:
        assert marker.read_text(encoding="utf-8") == "historical-proof\n"
        assert not mutation_log.exists(), (
            "capable or uncertain historical pairs must be a mutation-free reuse/failure"
        )


@pytest.mark.parametrize(
    ("probe_case", "expected_state"),
    [
        ("checksum_mismatch", "definitively_invalid"),
        ("sha256sum_absent", "probe_error"),
        ("sha256sum_io_error", "probe_error"),
        ("find_error", "probe_error"),
        ("find_malformed_output", "probe_error"),
        ("stat_error", "probe_error"),
        ("stat_mode_malformed", "probe_error"),
        ("unsafe_mode", "definitively_invalid"),
        ("python_error", "probe_error"),
        ("contract_mismatch", "definitively_invalid"),
    ],
)
def test_release_contract_classifier_separates_mismatch_from_tool_or_io_uncertainty(
    tmp_path: Path, probe_case: str, expected_state: str
) -> None:
    """The real classifier only calls a successfully confirmed mismatch invalid."""
    sha = "7" * 40
    lab_root = tmp_path / "miloco-lab"
    release = lab_root / "releases" / sha
    release.mkdir(parents=True)
    release_json = release / "release.json"
    release_json.write_text(
        '{"schema": 1, "git_sha": "'
        + (("8" * 40) if probe_case == "contract_mismatch" else sha)
        + '", "platform": "linux/amd64"}\n',
        encoding="utf-8",
    )
    payload = release / "remote-release.sh"
    payload.write_text("payload\n", encoding="utf-8")
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    if probe_case == "checksum_mismatch":
        digest = "0" * 64
    (release / "SHA256SUMS").write_text(
        f"{hashlib.sha256(release_json.read_bytes()).hexdigest()}  release.json\n"
        f"{digest}  remote-release.sh\n",
        encoding="utf-8",
    )

    source = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8").rsplit(
        '\nmain "$@"', maxsplit=1
    )[0]
    source = source.replace('readonly LAB_ROOT="/opt/miloco-lab"', f'readonly LAB_ROOT="{lab_root}"')
    normal_stat = (
        "stat() { case \"$2\" in '%u:%g') printf '0:0\\n' ;; '%a') "
        "case \"$3\" in */remote-release.sh) printf '555\\n' ;; *) "
        "[[ -d \"$3\" ]] && printf '755\\n' || printf '644\\n' ;; esac ;; *) return 74 ;; esac; }"
    )
    overrides = [normal_stat]
    if probe_case == "stat_error":
        overrides = ["stat() { return 74; }"]
    if probe_case == "stat_mode_malformed":
        overrides = [
            "stat() { case \"$2\" in '%u:%g') printf '0:0\\n' ;; '%a') printf 'not-a-mode\\n' ;; esac; }"
        ]
    if probe_case == "unsafe_mode":
        overrides = [
            "stat() { case \"$2\" in '%u:%g') printf '0:0\\n' ;; '%a') "
            "case \"$3\" in */remote-release.sh) printf '666\\n' ;; *) [[ -d \"$3\" ]] && printf '755\\n' || printf '644\\n' ;; esac ;; esac; }"
        ]
    if probe_case == "find_error":
        overrides.append("find() { return 74; }")
    if probe_case == "find_malformed_output":
        overrides.append("find() { printf '/outside-release\\n'; }")
    if probe_case == "python_error":
        overrides.append("python3() { return 74; }")
    if probe_case == "sha256sum_absent":
        overrides.append("sha256sum() { return 127; }")
    if probe_case == "sha256sum_io_error":
        overrides.append("sha256sum() { return 74; }")
    harness = tmp_path / f"contract-classifier-{probe_case}.sh"
    harness.write_text(
        source
        + "\n"
        + "\n".join(overrides)
        + "\n"
        + f'release_contract_state "{sha}"\n',
        encoding="utf-8",
    )
    harness.chmod(0o755)
    result = subprocess.run(
        [str(harness)], text=True, capture_output=True, check=False, timeout=5
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected_state


def _write_valid_classifier_release(lab_root: Path, sha: str) -> Path:
    release = lab_root / "releases" / sha
    release.mkdir(parents=True)
    release_json = release / "release.json"
    release_json.write_text(
        '{"schema":1,"git_sha":"'
        + sha
        + '","built_at":"2026-08-28T00:00:00Z","platform":"linux/amd64",'
        + '"artifacts":{"miloco":"miloco.whl","cli":"cli.whl",'
        + '"miot":"miot.whl","models":"models.tar.gz"}}\n',
        encoding="utf-8",
    )
    payload = release / "remote-release.sh"
    payload.write_text("payload\n", encoding="utf-8")
    payload.chmod(0o555)
    (release / "SHA256SUMS").write_text(
        f"{hashlib.sha256(release_json.read_bytes()).hexdigest()}  release.json\n"
        f"{hashlib.sha256(payload.read_bytes()).hexdigest()}  remote-release.sh\n",
        encoding="utf-8",
    )
    return release


def _run_release_classifier(
    tmp_path: Path,
    lab_root: Path,
    sha: str,
    *,
    shell_override: str = "",
    invocation: str | None = None,
) -> subprocess.CompletedProcess[str]:
    source = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8").rsplit(
        '\nmain "$@"', maxsplit=1
    )[0]
    source = source.replace('readonly LAB_ROOT="/opt/miloco-lab"', f'readonly LAB_ROOT="{lab_root}"')
    stat_override = (
        "stat() { case \"$2\" in '%u:%g') printf '0:0\\n' ;; '%a') "
        "case \"$3\" in */remote-release.sh|*/container-entrypoint.sh|*/acceptance/scripts/*.sh) "
        "printf '555\\n' ;; *) [[ -d \"$3\" ]] && printf '755\\n' || printf '644\\n' ;; "
        "esac ;; *) return 74 ;; esac; }"
    )
    harness = tmp_path / ("strict-classifier-" + hashlib.sha256(shell_override.encode()).hexdigest()[:8] + ".sh")
    harness.write_text(
        source
        + "\n"
        + stat_override
        + "\n"
        + shell_override
        + "\n"
        + (invocation or f'release_contract_state "{sha}"')
        + "\n",
        encoding="utf-8",
    )
    harness.chmod(0o755)
    return subprocess.run(
        [str(harness)], text=True, capture_output=True, check=False, timeout=5
    )


@pytest.mark.parametrize("find_case", ["empty", "partial", "duplicate"])
def test_release_classifier_treats_successful_incomplete_find_output_as_uncertain(
    tmp_path: Path, find_case: str
) -> None:
    """Catches a successful but non-bijective enumeration being called valid."""
    sha = "a" * 40
    lab_root = tmp_path / "miloco-lab"
    release = _write_valid_classifier_release(lab_root, sha)
    if find_case == "empty":
        replacement = "printf ''"
    elif find_case == "partial":
        replacement = f"printf '%s\\n' '{release}' '{release / 'release.json'}'"
    else:
        replacement = f"printf '%s\\n' '{release}' '{release}' '{release / 'release.json'}' '{release / 'SHA256SUMS'}' '{release / 'remote-release.sh'}'"
    override = (
        "find() { case \"$*\" in "
        "*'-type l'*|*'! -type d ! -type f'*) command find \"$@\" ;; "
        f"*'-xdev -print'*) {replacement} ;; "
        "*) command find \"$@\" ;; esac; }"
    )
    result = _run_release_classifier(tmp_path, lab_root, sha, shell_override=override)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "probe_error"


@pytest.mark.parametrize("hash_case", ["extra_line", "wrong_filename"])
def test_release_classifier_requires_exact_single_file_hash_output(
    tmp_path: Path, hash_case: str
) -> None:
    """Catches a digest prefix being accepted from malformed successful hash output."""
    sha = "b" * 40
    lab_root = tmp_path / "miloco-lab"
    _write_valid_classifier_release(lab_root, sha)
    if hash_case == "extra_line":
        suffix = "printf '%064d  extra\\n' 0"
    else:
        suffix = "printf '%064d  wrong-name\\n' 0"
    override = "sha256sum() { command sha256sum \"$@\"; " + suffix + "; }"
    result = _run_release_classifier(tmp_path, lab_root, sha, shell_override=override)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "probe_error"


@pytest.mark.parametrize("checksum_case", ["duplicate_path", "trailing_blank"])
def test_release_classifier_rejects_noncanonical_checksum_records(
    tmp_path: Path, checksum_case: str
) -> None:
    """Catches ambiguous checksum manifests being accepted as release proof."""
    sha = "c" * 40
    lab_root = tmp_path / "miloco-lab"
    release = _write_valid_classifier_release(lab_root, sha)
    checksum_file = release / "SHA256SUMS"
    content = checksum_file.read_text(encoding="utf-8")
    if checksum_case == "duplicate_path":
        content += content.splitlines(keepends=True)[0]
    else:
        content += "\n"
    checksum_file.write_text(content, encoding="utf-8")
    result = _run_release_classifier(tmp_path, lab_root, sha)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "definitively_invalid"


@pytest.mark.parametrize(
    "release_json",
    [
        '{"schema":1,"git_sha":"{sha}","platform":"linux/amd64"} trailing\n',
        '{"schema":1,"schema":1,"git_sha":"{sha}","platform":"linux/amd64"}\n',
        '{"schema":1,"git_sha":"{sha}","platform":"linux/amd64","platform":"other"}\n',
        '[{"schema":1,"git_sha":"{sha}","platform":"linux/amd64"}]\n',
    ],
)
def test_release_classifier_uses_strict_json_semantics(
    tmp_path: Path, release_json: str
) -> None:
    """Catches grep-compatible but invalid, duplicate, conflicting, or wrapped metadata."""
    sha = "d" * 40
    lab_root = tmp_path / "miloco-lab"
    release = _write_valid_classifier_release(lab_root, sha)
    metadata = release / "release.json"
    metadata.write_text(release_json.replace("{sha}", sha), encoding="utf-8")
    checksum_file = release / "SHA256SUMS"
    payload = release / "remote-release.sh"
    checksum_file.write_text(
        f"{hashlib.sha256(metadata.read_bytes()).hexdigest()}  release.json\n"
        f"{hashlib.sha256(payload.read_bytes()).hexdigest()}  remote-release.sh\n",
        encoding="utf-8",
    )
    result = _run_release_classifier(tmp_path, lab_root, sha)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "definitively_invalid"


@pytest.mark.parametrize(
    "path_case",
    ["lab_root", "releases_parent", "deploy_state", "records_parent", "accepted_parent"],
)
def test_release_classifier_never_calls_symlinked_ancestor_paths_capable(
    tmp_path: Path, path_case: str
) -> None:
    """Catches resolved-path-only validation that skips a raw ancestor symlink."""
    sha = "e" * 40
    visible_root = tmp_path / "miloco-lab"
    if path_case == "lab_root":
        actual_root = tmp_path / "actual-root"
        _write_valid_classifier_release(actual_root, sha)
        visible_root.symlink_to(actual_root, target_is_directory=True)
        lab_root = visible_root
    else:
        lab_root = visible_root
        _write_valid_classifier_release(lab_root, sha)
        if path_case == "releases_parent":
            actual_releases = tmp_path / "actual-releases"
            (lab_root / "releases").rename(actual_releases)
            (lab_root / "releases").symlink_to(actual_releases, target_is_directory=True)
        elif path_case == "deploy_state":
            actual_state = tmp_path / "actual-deploy-state"
            actual_state.mkdir()
            (lab_root / "deploy-state").symlink_to(actual_state, target_is_directory=True)
        else:
            deploy_state = lab_root / "deploy-state"
            deploy_state.mkdir()
            directory_name = "artifacts" if path_case == "records_parent" else "accepted"
            actual_records = deploy_state / f"actual-{directory_name}"
            actual_records.mkdir()
            (deploy_state / directory_name).symlink_to(actual_records, target_is_directory=True)
            other_name = "accepted" if directory_name == "artifacts" else "artifacts"
            (deploy_state / other_name).mkdir()
    invocation = None
    if path_case in {"deploy_state", "records_parent", "accepted_parent"}:
        invocation = (
            f'if classifier_capability_paths_state "{sha}"; then printf valid; '
            "else status=$?; [[ $status -eq 1 ]] && printf definitively_invalid "
            "|| printf probe_error; fi"
        )
    result = _run_release_classifier(tmp_path, lab_root, sha, invocation=invocation)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() != "valid"


@pytest.mark.parametrize("record_kind", ["artifact", "acceptance"])
def test_fixed_records_reject_a_trailing_blank_record(
    tmp_path: Path, record_kind: str
) -> None:
    """Catches command-substitution newline stripping that hides an extra record."""
    sha = "f" * 40
    lab_root = tmp_path / "miloco-lab"
    artifacts = lab_root / "deploy-state" / "artifacts"
    accepted = lab_root / "deploy-state" / "accepted"
    artifacts.mkdir(parents=True)
    accepted.mkdir()
    if record_kind == "artifact":
        (artifacts / sha).write_text(
            "schema=1\n"
            f"git_sha={sha}\n"
            f"archive_sha256={'1' * 64}\n"
            f"controller_sha256={'2' * 64}\n"
            f"allowlist_sha256={hashlib.sha256(ALLOWLIST.read_bytes()).hexdigest()}\n\n",
            encoding="utf-8",
        )
        reader = "read_artifact_record"
    else:
        (accepted / sha).write_text(
            "schema=1\n"
            f"archive_sha256={'1' * 64}\n"
            f"runtime_image_id=sha256:{'2' * 64}\n"
            f"acceptance_image_id=sha256:{'3' * 64}\n\n",
            encoding="utf-8",
        )
        reader = "read_acceptance_marker"
    source = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8").rsplit(
        '\nmain "$@"', maxsplit=1
    )[0]
    source = source.replace('readonly LAB_ROOT="/opt/miloco-lab"', f'readonly LAB_ROOT="{lab_root}"')
    harness = tmp_path / "strict-record.sh"
    harness.write_text(
        source
        + "\nstat() { case \"$2\" in '%u:%g') printf '0:0\\n' ;; '%a') "
        + "[[ -d \"$3\" ]] && printf '755\\n' || printf '644\\n' ;; esac; }\n"
        + f'if {reader} "{sha}"; then printf valid; else printf invalid; fi\n',
        encoding="utf-8",
    )
    harness.chmod(0o755)
    result = subprocess.run(
        [str(harness)], text=True, capture_output=True, check=False, timeout=5
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "invalid"


def test_exact_valid_classifier_path_and_metadata_remain_capable(tmp_path: Path) -> None:
    """Catches strict parsing that rejects the canonical release and proof records."""
    sha = "1" * 40
    archive_digest = "2" * 64
    runtime_id = "sha256:" + "3" * 64
    acceptance_id = "sha256:" + "4" * 64
    lab_root = tmp_path / "miloco-lab"
    _write_valid_classifier_release(lab_root, sha)
    artifacts = lab_root / "deploy-state" / "artifacts"
    accepted = lab_root / "deploy-state" / "accepted"
    artifacts.mkdir(parents=True)
    accepted.mkdir()
    (artifacts / sha).write_text(
        "schema=1\n"
        f"git_sha={sha}\n"
        f"archive_sha256={archive_digest}\n"
        f"controller_sha256={'5' * 64}\n"
        f"allowlist_sha256={hashlib.sha256(ALLOWLIST.read_bytes()).hexdigest()}\n",
        encoding="utf-8",
    )
    (accepted / sha).write_text(
        "schema=1\n"
        f"archive_sha256={archive_digest}\n"
        f"runtime_image_id={runtime_id}\n"
        f"acceptance_image_id={acceptance_id}\n",
        encoding="utf-8",
    )
    image_override = (
        "image_reference_state() { case \"$1\" in miloco-lab-acceptance:*) "
        f"printf 'present:{acceptance_id}\\n' ;; *) printf 'present:{runtime_id}\\n' ;; esac; }}"
    )
    result = _run_release_classifier(
        tmp_path,
        lab_root,
        sha,
        shell_override=image_override,
        invocation=f'release_capability "{sha}"',
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "capable"


@pytest.mark.parametrize(
    ("record_case", "expected_state"),
    [
        ("missing", "definitively_invalid"),
        ("malformed", "definitively_invalid"),
        ("read_error", "probe_error"),
        ("stat_error", "probe_error"),
    ],
)
def test_release_capability_propagates_record_semantics_and_read_uncertainty(
    tmp_path: Path, record_case: str, expected_state: str
) -> None:
    sha = "5" * 40
    lab_root = tmp_path / "miloco-lab"
    artifacts = lab_root / "deploy-state" / "artifacts"
    accepted = lab_root / "deploy-state" / "accepted"
    artifacts.mkdir(parents=True)
    accepted.mkdir(parents=True)
    artifact_record = artifacts / sha
    if record_case != "missing":
        artifact_record.write_text(
            "malformed\n"
            if record_case == "malformed"
            else (
                "schema=1\n"
                f"git_sha={sha}\n"
                f"archive_sha256={'a' * 64}\n"
                f"controller_sha256={'b' * 64}\n"
                f"allowlist_sha256={hashlib.sha256(ALLOWLIST.read_bytes()).hexdigest()}\n"
            ),
            encoding="utf-8",
        )
    source = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8").rsplit(
        '\nmain "$@"', maxsplit=1
    )[0]
    source = source.replace('readonly LAB_ROOT="/opt/miloco-lab"', f'readonly LAB_ROOT="{lab_root}"')
    overrides = [
        "stat() { case \"$2\" in '%u:%g') printf '0:0\\n' ;; '%a') "
        "[[ -d \"$3\" ]] && printf '755\\n' || printf '644\\n' ;; *) return 74 ;; esac; }"
    ]
    if record_case == "stat_error":
        overrides = ["stat() { return 74; }"]
    if record_case == "read_error":
        overrides.append("read_file_content() { return 74; }")
    harness = tmp_path / f"record-classifier-{record_case}.sh"
    harness.write_text(
        source
        + "\n"
        + "\n".join(overrides)
        + "\nclassifier_capability_paths_state() { return 0; }"
        + "\n"
        + f'release_capability "{sha}"\n',
        encoding="utf-8",
    )
    harness.chmod(0o755)
    result = subprocess.run(
        [str(harness)], text=True, capture_output=True, check=False, timeout=5
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected_state


@pytest.mark.parametrize("interrupt_stage", ["build", "acceptance"])
def test_unaccepted_candidate_signal_cleanup_is_armed_before_first_mutation(
    tmp_path: Path, interrupt_stage: str
) -> None:
    """Catches session loss leaving unaccepted tags or touching another protected proof."""
    sha = "2" * 40
    protected_sha = "3" * 40
    lab_root = tmp_path / "miloco-lab"
    deploy_state = lab_root / "deploy-state"
    accepted = deploy_state / "accepted"
    accepted.mkdir(parents=True)
    (deploy_state / "current").write_text(f"{protected_sha}\n", encoding="utf-8")
    protected_marker = accepted / protected_sha
    protected_marker.write_text("protected-proof\n", encoding="utf-8")
    candidate_marker = accepted / sha
    candidate_marker.write_text("stale-unprotected-proof\n", encoding="utf-8")
    call_log = tmp_path / f"signal-{interrupt_stage}.log"
    trap_log = tmp_path / f"signal-{interrupt_stage}-trap.log"
    source = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8").rsplit(
        '\nmain "$@"', maxsplit=1
    )[0]
    source = source.replace('readonly LAB_ROOT="/opt/miloco-lab"', f'readonly LAB_ROOT="{lab_root}"')
    harness = tmp_path / f"signal-{interrupt_stage}.sh"
    harness.write_text(
        source
        + "\nrequire_safe_directory() { return 0; }\n"
        + "require_safe_record() { return 0; }\n"
        + "release_capability() { printf 'definitively_invalid\\n'; }\n"
        + "invalidate_acceptance() {\n"
        + f"  [[ -s '{trap_log}' ]] || trap -p EXIT > '{trap_log}'\n"
        + f"  rm -f -- '{candidate_marker}'\n"
        + "}\n"
        + "docker_command() {\n"
        + f"  printf 'docker:%s\\n' \"$*\" >> '{call_log}'\n"
        + f"  case \"$*\" in *' build '*'--target runtime'*) [[ '{interrupt_stage}' != build ]] || {{ printf 'signal-build\\n' >> '{call_log}'; kill -TERM \"$$\"; }} ;; esac\n"
        + f"  case \"$*\" in *' run --rm '*) [[ '{interrupt_stage}' != acceptance ]] || {{ printf 'signal-acceptance\\n' >> '{call_log}'; kill -TERM \"$$\"; }} ;; esac\n"
        + "  case \"$*\" in *' image inspect '*) printf 'sha256:%064d\\n' 0 ;; esac\n"
        + "  return 0\n"
        + "}\n"
        + f'build_images_and_accept "ai-lab01.esxi" "{sha}" "{lab_root}/releases/{sha}"\n',
        encoding="utf-8",
    )
    harness.chmod(0o755)
    result = subprocess.run(
        [str(harness)], text=True, capture_output=True, check=False, timeout=5
    )
    assert result.returncode != 0
    assert trap_log.read_text(encoding="utf-8").strip(), (
        "EXIT cleanup must be armed before acceptance-marker invalidation"
    )
    calls = call_log.read_text(encoding="utf-8")
    signal_line = f"signal-{interrupt_stage}"
    assert signal_line in calls
    after_signal = calls.split(signal_line, maxsplit=1)[1]
    assert f"miloco-lab-candidate:{sha}" in calls
    assert f"miloco-lab-acceptance-candidate:{sha}" in after_signal
    assert not candidate_marker.exists()
    assert protected_marker.read_text(encoding="utf-8") == "protected-proof\n"
    assert protected_sha not in calls


def test_candidate_cleanup_disarms_traps_before_clearing_cleanup_identity() -> None:
    """Catches a signal observing an empty cleanup SHA while the EXIT trap remains armed."""
    remote = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8")
    disarm = remote.split("disarm_candidate_cleanup()", maxsplit=1)[1].split(
        "\n}", maxsplit=1
    )[0]
    assert disarm.index("trap - EXIT HUP INT TERM") < disarm.index(
        'candidate_cleanup_sha=""'
    )


def test_receive_retry_reuses_only_the_same_verified_artifact_without_extraction(tmp_path: Path) -> None:
    """Catches failed acceptance leaving the exact SHA permanently undeployable on retry."""
    sha = "9" * 40
    digest = "a" * 64
    lab_root = tmp_path / "miloco-lab"
    release = lab_root / "releases" / sha
    incoming = lab_root / "incoming"
    release.mkdir(parents=True)
    incoming.mkdir(parents=True)
    source = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8")
    source = source.rsplit('\nmain "$@"', maxsplit=1)[0]
    source = source.replace('readonly LAB_ROOT="/opt/miloco-lab"', f'readonly LAB_ROOT="{lab_root}"')
    source = source.replace(
        '"/opt/miloco-lab/releases/$sha"', f'"{lab_root}/releases/$sha"'
    )
    call_log = tmp_path / "retry.log"
    harness = tmp_path / "receive-retry.sh"
    harness.write_text(
        source
        + "\nrequire_root() { :; }\n"
        + "acquire_transition_lock() { :; }\n"
        + f"verify_archive_digest() {{ printf 'digest\\n' >> '{call_log}'; }}\n"
        + f"validate_archive_members() {{ printf 'members\\n' >> '{call_log}'; }}\n"
        + f"verify_release_tree() {{ printf 'release\\n' >> '{call_log}'; }}\n"
        + f"read_artifact_record() {{ artifact_archive_digest='{digest}'; artifact_controller_digest='{digest}'; artifact_allowlist_digest='{hashlib.sha256(ALLOWLIST.read_bytes()).hexdigest()}'; }}\n"
        + f'receive_release_locked "ai-lab01.esxi" "{sha}" "{digest}" "{digest}" "{hashlib.sha256(ALLOWLIST.read_bytes()).hexdigest()}"\n',
        encoding="utf-8",
    )
    harness.chmod(0o755)
    bin_dir = tmp_path / "retry-bin"
    bin_dir.mkdir()
    for command in ("install", "chown", "chmod"):
        _write_stub(bin_dir, command, call_log)
    _write_stub(bin_dir, "tar", call_log, "exit 99")
    result = subprocess.run(
        [str(harness)],
        input="same archive bytes",
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode == 0, result.stderr
    calls = call_log.read_text(encoding="utf-8")
    assert "digest\nmembers\nrelease\n" in calls
    assert not any(line.startswith("tar ") for line in calls.splitlines()), (
        "an identical verified retry must not extract over the exact release"
    )


def test_retention_counts_only_two_rollback_capable_historical_pairs(tmp_path: Path) -> None:
    """Catches failed debris consuming retention slots or evicting a usable previous pair."""
    current, debris, previous, retained, expired = (character * 40 for character in "cdbef")
    lab_root = tmp_path / "miloco-lab"
    releases = lab_root / "releases"
    state = lab_root / "deploy-state"
    releases.mkdir(parents=True)
    state.mkdir(parents=True)
    for candidate in (current, debris, previous, retained, expired):
        (releases / candidate).mkdir()
    (state / "previous").write_text(f"{previous}\n", encoding="utf-8")
    source = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8")
    source = source.rsplit('\nmain "$@"', maxsplit=1)[0]
    source = source.replace('readonly LAB_ROOT="/opt/miloco-lab"', f'readonly LAB_ROOT="{lab_root}"')
    removal_log = tmp_path / "removed.log"
    harness = tmp_path / "retention.sh"
    harness.write_text(
        source
        + "\nrequire_safe_record() { return 0; }\n"
        + "\nrelease_capability() {\n"
        + f"  case \"$1\" in {current}|{previous}|{retained}|{expired}) printf 'capable\\n' ;; *) printf 'definitively_invalid\\n' ;; esac\n"
        + "}\n"
        + f"remove_release_pair() {{ printf '%s\\n' \"$1\" >> '{removal_log}'; }}\n"
        + f"retain_rollback_history '{current}'\n",
        encoding="utf-8",
    )
    harness.chmod(0o755)
    bin_dir = tmp_path / "retention-bin"
    bin_dir.mkdir()
    find_stub = bin_dir / "find"
    find_stub.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' '5 {releases / current}' '4 {releases / debris}' '3 {releases / previous}' '2 {releases / retained}' '1 {releases / expired}'\n",
        encoding="utf-8",
    )
    find_stub.chmod(0o755)
    result = subprocess.run(
        [str(harness)],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode == 0, result.stderr
    removed = set(removal_log.read_text(encoding="utf-8").splitlines())
    assert removed == {debris, expired}


def test_retention_probe_error_stops_without_deleting_any_pair(tmp_path: Path) -> None:
    """Catches a transient Docker/verification error being classified as removable debris."""
    current, previous, transient, later = (character * 40 for character in "1234")
    lab_root = tmp_path / "miloco-lab"
    releases = lab_root / "releases"
    deploy_state = lab_root / "deploy-state"
    releases.mkdir(parents=True)
    deploy_state.mkdir(parents=True)
    for candidate in (current, previous, transient, later):
        (releases / candidate).mkdir()
    (deploy_state / "previous").write_text(previous + "\n", encoding="utf-8")
    source = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8").rsplit(
        '\nmain "$@"', maxsplit=1
    )[0]
    source = source.replace('readonly LAB_ROOT="/opt/miloco-lab"', f'readonly LAB_ROOT="{lab_root}"')
    removal_log = tmp_path / "transient-removal.log"
    harness = tmp_path / "retention-probe-error.sh"
    harness.write_text(
        source
        + "\nrequire_safe_record() { return 0; }\n"
        + "\nrelease_capability() {\n"
        + f"  case \"$1\" in {current}|{previous}) printf 'capable\\n' ;; {transient}) printf 'probe_error\\n' ;; *) printf 'definitively_invalid\\n' ;; esac\n"
        + "}\n"
        + f"remove_release_pair() {{ printf '%s\\n' \"$1\" >> '{removal_log}'; }}\n"
        + f"retain_rollback_history '{current}'\n",
        encoding="utf-8",
    )
    harness.chmod(0o755)
    bin_dir = tmp_path / "retention-probe-bin"
    bin_dir.mkdir()
    find_stub = bin_dir / "find"
    find_stub.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' '4 {releases / current}' '3 {releases / previous}' '2 {releases / transient}' '1 {releases / later}'\n",
        encoding="utf-8",
    )
    find_stub.chmod(0o755)
    result = subprocess.run(
        [str(harness)],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode != 0
    assert "probe_error" in result.stderr
    assert not removal_log.exists(), "cleanup must stop before any delete on probe error"


def test_retention_malformed_listing_stops_without_deleting_any_pair(tmp_path: Path) -> None:
    current = "8" * 40
    lab_root = tmp_path / "miloco-lab"
    releases = lab_root / "releases"
    releases.mkdir(parents=True)
    (releases / current).mkdir()
    source = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8").rsplit(
        '\nmain "$@"', maxsplit=1
    )[0]
    source = source.replace('readonly LAB_ROOT="/opt/miloco-lab"', f'readonly LAB_ROOT="{lab_root}"')
    removal_log = tmp_path / "malformed-listing-removal.log"
    harness = tmp_path / "retention-malformed-listing.sh"
    harness.write_text(
        source
        + "\nrelease_capability() { printf 'capable\\n'; }\n"
        + f"remove_release_pair() {{ printf '%s\\n' \"$1\" >> '{removal_log}'; }}\n"
        + f"retain_rollback_history '{current}'\n",
        encoding="utf-8",
    )
    harness.chmod(0o755)
    bin_dir = tmp_path / "retention-malformed-bin"
    bin_dir.mkdir()
    find_stub = bin_dir / "find"
    find_stub.write_text(
        "#!/usr/bin/env bash\nprintf 'malformed-tool-output\\n'\n",
        encoding="utf-8",
    )
    find_stub.chmod(0o755)
    result = subprocess.run(
        [str(harness)],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode != 0
    assert "probe_error" in result.stderr
    assert not removal_log.exists()


def test_remove_pair_protects_state_and_verifies_full_removal(tmp_path: Path) -> None:
    """Catches release-first deletion, state-target deletion, or leftover records/images."""
    current, previous, candidate = (character * 40 for character in "567")
    lab_root = tmp_path / "miloco-lab"
    release = lab_root / "releases" / candidate
    artifacts = lab_root / "deploy-state" / "artifacts"
    accepted = lab_root / "deploy-state" / "accepted"
    release.mkdir(parents=True)
    artifacts.mkdir(parents=True)
    accepted.mkdir(parents=True)
    (lab_root / "deploy-state" / "current").write_text(current + "\n", encoding="utf-8")
    (lab_root / "deploy-state" / "previous").write_text(previous + "\n", encoding="utf-8")
    (artifacts / candidate).write_text("record\n", encoding="utf-8")
    (accepted / candidate).write_text("marker\n", encoding="utf-8")
    source = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8").rsplit(
        '\nmain "$@"', maxsplit=1
    )[0]
    source = source.replace('readonly LAB_ROOT="/opt/miloco-lab"', f'readonly LAB_ROOT="{lab_root}"')
    source = source.replace(
        '"/opt/miloco-lab/releases/$sha"', f'"{lab_root}/releases/$sha"'
    )
    call_log = tmp_path / "full-removal.log"
    harness = tmp_path / "full-removal.sh"
    harness.write_text(
        source
        + "\nrequire_safe_directory() { return 0; }\n"
        + "require_safe_record() { return 0; }\n"
        + "docker_command() {\n"
        + f"  printf 'docker:%s\\n' \"$*\" >> '{call_log}'\n"
        + "  case \"$*\" in *' image ls '*) return 0 ;; *) return 0 ;; esac\n"
        + "}\n"
        + 'remove_release_pair "$1"\n',
        encoding="utf-8",
    )
    harness.chmod(0o755)
    result = subprocess.run(
        [str(harness), candidate], text=True, capture_output=True, check=False, timeout=5
    )
    assert result.returncode == 0, result.stderr
    assert not release.exists()
    assert not (artifacts / candidate).exists()
    assert not (accepted / candidate).exists()
    calls = call_log.read_text(encoding="utf-8")
    assert "image rm" in calls and calls.count("image ls") >= 2

    call_count = len(calls.splitlines())
    protected = subprocess.run(
        [str(harness), current],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert protected.returncode != 0
    assert len(call_log.read_text(encoding="utf-8").splitlines()) == call_count


def test_remove_pair_prevalidates_child_records_before_images(tmp_path: Path) -> None:
    """Catches cleanup mutating image state before discovering an escaping record symlink."""
    sha = "9" * 40
    lab_root = tmp_path / "miloco-lab"
    artifacts = lab_root / "deploy-state" / "artifacts"
    accepted = lab_root / "deploy-state" / "accepted"
    outside = tmp_path / "outside-record"
    artifacts.mkdir(parents=True)
    accepted.mkdir(parents=True)
    outside.write_text("outside\n", encoding="utf-8")
    (artifacts / sha).symlink_to(outside)
    source = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8").rsplit(
        '\nmain "$@"', maxsplit=1
    )[0]
    source = source.replace('readonly LAB_ROOT="/opt/miloco-lab"', f'readonly LAB_ROOT="{lab_root}"')
    source = source.replace(
        '"/opt/miloco-lab/releases/$sha"', f'"{lab_root}/releases/$sha"'
    )
    docker_log = tmp_path / "prevalidation-docker.log"
    harness = tmp_path / "prevalidate-removal.sh"
    harness.write_text(
        source
        + "\nrequire_safe_directory() { return 0; }\n"
        + f"docker_command() {{ printf 'docker\\n' >> '{docker_log}'; return 0; }}\n"
        + f"remove_release_pair '{sha}'\n",
        encoding="utf-8",
    )
    harness.chmod(0o755)
    result = subprocess.run(
        [str(harness)], text=True, capture_output=True, check=False, timeout=5
    )
    assert result.returncode != 0
    assert not docker_log.exists(), "all filesystem targets must validate before image mutation"


def test_status_rejects_current_record_symlink_without_docker(tmp_path: Path) -> None:
    """Catches read-only status treating a child record symlink as ordinary undeployed state."""
    lab_root = tmp_path / "miloco-lab"
    deploy_state = lab_root / "deploy-state"
    outside = tmp_path / "outside-current"
    deploy_state.mkdir(parents=True)
    outside.write_text("a" * 40 + "\n", encoding="utf-8")
    (deploy_state / "current").symlink_to(outside)
    source = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8").rsplit(
        '\nmain "$@"', maxsplit=1
    )[0]
    source = source.replace('readonly LAB_ROOT="/opt/miloco-lab"', f'readonly LAB_ROOT="{lab_root}"')
    docker_log = tmp_path / "status-symlink-docker.log"
    harness = tmp_path / "status-record-symlink.sh"
    harness.write_text(
        source
        + "\nrequire_root() { :; }\n"
        + f"docker_command() {{ printf docker > '{docker_log}'; }}\n"
        + 'status_release "ai-lab01.esxi"\n',
        encoding="utf-8",
    )
    harness.chmod(0o755)
    result = subprocess.run(
        [str(harness)], text=True, capture_output=True, check=False, timeout=5
    )
    assert result.returncode == 4
    assert "unsafe" in result.stderr
    assert not docker_log.exists()


def test_status_rejects_raw_lab_root_symlink_without_docker(tmp_path: Path) -> None:
    """Catches read-only status resolving through an attacker-repointable lab root."""
    actual_root = tmp_path / "actual-root"
    actual_root.mkdir()
    lab_root = tmp_path / "miloco-lab"
    lab_root.symlink_to(actual_root, target_is_directory=True)
    source = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8").rsplit(
        '\nmain "$@"', maxsplit=1
    )[0].replace(
        'readonly LAB_ROOT="/opt/miloco-lab"', f'readonly LAB_ROOT="{lab_root}"'
    )
    docker_log = tmp_path / "status-root-symlink-docker.log"
    harness = tmp_path / "status-root-symlink.sh"
    harness.write_text(
        source
        + "\nid() { printf '0\\n'; }\n"
        + f"docker() {{ printf called >> '{docker_log}'; return 99; }}\n"
        + 'main "$@"\n',
        encoding="utf-8",
    )
    harness.chmod(0o755)
    result = subprocess.run(
        [str(harness), "status", "ai-lab01.esxi"],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 4
    assert "unsafe" in result.stderr or "symlink" in result.stderr
    assert not docker_log.exists()


def test_transaction_rejects_child_symlink_before_docker(tmp_path: Path) -> None:
    """Catches accepted/artifact/incoming child paths escaping the root-owned lab tree."""
    lab_root = tmp_path / "miloco-lab"
    outside = tmp_path / "outside"
    outside.mkdir()
    (lab_root / "deploy-state").mkdir(parents=True)
    (lab_root / "deploy-state" / "accepted").symlink_to(outside, target_is_directory=True)
    source = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8").rsplit(
        '\nmain "$@"', maxsplit=1
    )[0]
    source = source.replace('readonly LAB_ROOT="/opt/miloco-lab"', f'readonly LAB_ROOT="{lab_root}"')
    harness = tmp_path / "child-symlink.sh"
    harness.write_text(source + "\nprepare_transaction_paths\n", encoding="utf-8")
    harness.chmod(0o755)
    bin_dir = tmp_path / "child-symlink-bin"
    bin_dir.mkdir()
    docker_log = tmp_path / "child-symlink-docker.log"
    _write_stub(bin_dir, "docker", docker_log)
    stat_stub = bin_dir / "stat"
    stat_stub.write_text("#!/usr/bin/env bash\nprintf '0:0\\n'\n", encoding="utf-8")
    stat_stub.chmod(0o755)
    result = subprocess.run(
        [str(harness)],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode == 4
    assert "symlink" in result.stderr
    assert not docker_log.exists()


def test_health_deadline_has_no_hidden_kill_grace() -> None:
    """Catches timeout kill-after grace extending a nominal 120-second health deadline."""
    remote = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8")
    assert "--kill-after" not in remote
    assert remote.count("--signal=KILL") >= 3
    health = remote.split("wait_for_health()", maxsplit=1)[1].split("\n}", maxsplit=1)[0]
    assert health.count("deadline - SECONDS") >= 4


def test_post_commit_retention_failure_keeps_activation_success(tmp_path: Path) -> None:
    """Catches cleanup failure being reported as activation or rollback failure after commit."""
    sha = "8" * 40
    lab_root = tmp_path / "miloco-lab"
    (lab_root / "deploy-state").mkdir(parents=True)
    source = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8").rsplit(
        '\nmain "$@"', maxsplit=1
    )[0]
    source = source.replace('readonly LAB_ROOT="/opt/miloco-lab"', f'readonly LAB_ROOT="{lab_root}"')
    harness = tmp_path / "activated-cleanup-failed.sh"
    harness.write_text(
        source
        + "\nrelease_capability() { printf 'capable\\n'; }\n"
        + "install() { :; }\n"
        + "atomic_write() { :; }\n"
        + "arm_transition() { :; }\n"
        + "commit_transition() { :; }\n"
        + "compose_up() { return 0; }\n"
        + "wait_for_health() { return 0; }\n"
        + "retain_rollback_history() { return 17; }\n"
        + f'activate_release "ai-lab01.esxi" "{sha}"\n',
        encoding="utf-8",
    )
    harness.chmod(0o755)
    result = subprocess.run(
        [str(harness)], text=True, capture_output=True, check=False, timeout=5
    )
    assert result.returncode == 0, result.stderr
    assert "activated_cleanup_failed" in result.stderr
    assert "rollback_failed" not in result.stderr


def test_failure_evidence_never_reads_or_emits_application_logs(tmp_path: Path) -> None:
    """Catches credential-shaped application output crossing the deployment channel."""
    remote = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8")
    assert " logs " not in remote
    assert "sanitize_logs" not in remote
    evidence = remote.split("collect_failure_evidence()", maxsplit=1)[1].split("\n}", maxsplit=1)[0]
    assert "probe_http_status" in evidence
    assert "container_health_status" in evidence

    source = remote.rsplit('\nmain "$@"', maxsplit=1)[0]
    harness = tmp_path / "evidence.sh"
    harness.write_text(
        source
        + "\ncompose_container_id() { printf 'Bearer super-secret'; }\n"
        + "container_health_status() { printf 'https://user:pass@example.invalid'; }\n"
        + "probe_http_status() { printf 'Cookie=session-secret'; }\n"
        + 'collect_failure_evidence "ai-lab01.esxi" "' + "6" * 40 + '"\n',
        encoding="utf-8",
    )
    harness.chmod(0o755)
    result = subprocess.run(
        [str(harness)], text=True, capture_output=True, check=False, timeout=5
    )
    combined = result.stdout + result.stderr
    for secret_fragment in ("Bearer", "super-secret", "user:pass", "Cookie", "session-secret"):
        assert secret_fragment not in combined
    assert "health=unknown" in combined
    assert "http=000" in combined


def test_health_probe_uses_remaining_budget_for_cli_and_http_timeouts() -> None:
    """Catches one probe call exceeding the total 120-second health window."""
    remote = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8")
    health = remote.split("wait_for_health()", maxsplit=1)[1].split("\n}", maxsplit=1)[0]
    assert "remaining" in health
    assert "probe_http_status" in health
    probe = remote.split("probe_http_status()", maxsplit=1)[1].split("\n}", maxsplit=1)[0]
    assert "--connect-timeout" in probe
    assert "--max-time" in probe
    assert "remaining" in probe


@pytest.mark.parametrize(
    ("tar_type", "label"),
    [
        ("p", "fifo"),
        ("b", "device"),
        ("c", "device"),
        ("s", "socket"),
        ("h", "hardlink"),
        ("l", "symlink"),
    ],
)
def test_receive_rejects_special_or_link_members_before_extraction(
    tmp_path: Path, tar_type: str, label: str
) -> None:
    """Catches archive members that can materialize outside regular release files."""
    sha = "1" * 40
    digest = "2" * 64
    lab_root = tmp_path / "miloco-lab"
    remote_copy = tmp_path / "remote-release.sh"
    remote_source = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8").rsplit(
        '\nmain "$@"', maxsplit=1
    )[0]
    remote_source = remote_source.replace(
        'readonly LAB_ROOT="/opt/miloco-lab"', f'readonly LAB_ROOT="{lab_root}"'
    ).replace('"/opt/miloco-lab/releases/$sha"', f'"{lab_root}/releases/$sha"')
    remote_copy.write_text(
        remote_source
        + f'\nreceive_release_locked "ai-lab01.esxi" "{sha}" "{digest}" "{digest}" "{hashlib.sha256(ALLOWLIST.read_bytes()).hexdigest()}"\n',
        encoding="utf-8",
    )
    remote_copy.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    call_log = tmp_path / "calls.log"
    _write_stub(bin_dir, "id", call_log, 'printf "0\\n"')
    install_stub = bin_dir / "install"
    install_stub.write_text(
        "#!/usr/bin/env bash\n"
        f"printf 'install %s\\n' \"$*\" >> '{call_log}'\n"
        "target=\"${!#}\"\n"
        "/bin/mkdir -p -- \"$target\"\n",
        encoding="utf-8",
    )
    install_stub.chmod(0o755)
    _write_stub(bin_dir, "flock", call_log)
    _write_stub(bin_dir, "chown", call_log)
    _write_stub(bin_dir, "chmod", call_log)
    sha_stub = bin_dir / "sha256sum"
    sha_stub.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s  %s\\n' '{digest}' \"$1\"\n",
        encoding="utf-8",
    )
    sha_stub.chmod(0o755)
    tar_stub = bin_dir / "tar"
    tar_stub.write_text(
        "#!/usr/bin/env bash\n"
        f"printf 'tar %s\\n' \"$*\" >> '{call_log}'\n"
        "case \"$*\" in\n"
        f"  *--verbose*) printf '%s\\n' '{tar_type}rw------- 0/0 0 2026-01-01 00:00 ./unsafe' ;;\n"
        "  *--list*) printf '%s\\n' './unsafe' ;;\n"
        "  *--extract*) exit 91 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    tar_stub.chmod(0o755)
    result = subprocess.run(
        [str(remote_copy)],
        input=b"archive-bytes",
        capture_output=True,
        check=False,
        timeout=5,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode == 4
    assert label.encode() in result.stderr
    assert not (lab_root / "releases" / sha).exists()
    assert "--extract" not in call_log.read_text(encoding="utf-8")


def test_remote_status_and_unknown_rollback_are_safe_under_stubs(tmp_path: Path) -> None:
    """Catches state creation by status and Docker access before rollback release proof."""
    lab_root = tmp_path / "miloco-lab"
    remote_copy = tmp_path / "remote-release.sh"
    remote_copy.write_text(
        REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8").replace(
            'readonly LAB_ROOT="/opt/miloco-lab"',
            f'readonly LAB_ROOT="{lab_root}"',
        ).replace(
            '"/opt/miloco-lab/releases/$sha"',
            f'"{lab_root}/releases/$sha"',
        ),
        encoding="utf-8",
    )
    remote_copy.chmod(0o755)
    bin_dir = tmp_path / "remote-bin"
    bin_dir.mkdir()
    external_log = tmp_path / "remote-external.log"
    _write_stub(bin_dir, "id", external_log, 'if [ "$1" = "-u" ]; then printf "0\\n"; fi')
    _write_stub(bin_dir, "docker", external_log, "exit 99")
    install_stub = bin_dir / "install"
    install_stub.write_text(
        "#!/usr/bin/env bash\n"
        f"printf 'install %s\\n' \"$*\" >> '{external_log}'\n"
        "target=\"${!#}\"\n/bin/mkdir -p -- \"$target\"\n",
        encoding="utf-8",
    )
    install_stub.chmod(0o755)
    _write_stub(bin_dir, "flock", external_log)
    _write_stub(bin_dir, "stat", external_log, 'printf "0:0\\n"')
    _write_stub(bin_dir, "chown", external_log)
    _write_stub(bin_dir, "chmod", external_log)
    realpath_stub = bin_dir / "realpath"
    realpath_stub.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"${1:-}\" == -e ]]; then shift; fi\n"
        "exec /bin/realpath \"$1\"\n",
        encoding="utf-8",
    )
    realpath_stub.chmod(0o755)
    environment = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}

    status = subprocess.run(
        [str(remote_copy), "status", "ai-lab01.esxi"],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
        env=environment,
    )
    assert status.returncode == 0, status.stderr
    assert "not_deployed" in status.stdout
    assert not lab_root.exists()
    assert "docker" not in external_log.read_text(encoding="utf-8")

    external_log.unlink()
    rollback = subprocess.run(
        [str(remote_copy), "rollback", "ai-lab01.esxi", "0" * 40],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
        env=environment,
    )
    assert rollback.returncode == 4
    assert "not verified and acceptance-approved" in rollback.stderr
    assert "docker" not in external_log.read_text(encoding="utf-8")


def test_transition_lock_conflict_exits_before_state_machine(tmp_path: Path) -> None:
    """Catches concurrent deploy/rollback transitions entering without one host lock."""
    lab_root = tmp_path / "miloco-lab"
    reached = tmp_path / "reached"
    source = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8")
    source = source.rsplit('\nmain "$@"', maxsplit=1)[0]
    source = source.replace('readonly LAB_ROOT="/opt/miloco-lab"', f'readonly LAB_ROOT="{lab_root}"')
    harness = tmp_path / "lock-harness.sh"
    harness.write_text(source + f'\nacquire_transition_lock\nprintf reached > "{reached}"\n', encoding="utf-8")
    harness.chmod(0o755)
    bin_dir = tmp_path / "bin-lock"
    bin_dir.mkdir()
    call_log = tmp_path / "lock-calls.log"
    install_stub = bin_dir / "install"
    install_stub.write_text(
        "#!/usr/bin/env bash\n"
        f"printf 'install %s\\n' \"$*\" >> '{call_log}'\n"
        "target=\"${!#}\"\n/bin/mkdir -p -- \"$target\"\n",
        encoding="utf-8",
    )
    install_stub.chmod(0o755)
    _write_stub(bin_dir, "flock", call_log, "exit 1")
    _write_stub(bin_dir, "stat", call_log, 'printf "0:0\\n"')
    _write_stub(bin_dir, "chown", call_log)
    _write_stub(bin_dir, "chmod", call_log)
    realpath_stub = bin_dir / "realpath"
    realpath_stub.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"${1:-}\" == -e ]]; then shift; fi\n"
        "exec /bin/realpath \"$1\"\n",
        encoding="utf-8",
    )
    realpath_stub.chmod(0o755)
    result = subprocess.run(
        [str(harness)],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode == 6
    assert "transition is locked" in result.stderr
    assert not reached.exists()


@pytest.mark.parametrize(("recovery_result", "expected_exit"), [(0, 143), (1, 70)])
def test_armed_signal_compensation_restores_or_reports_rollback_failure(
    tmp_path: Path, recovery_result: int, expected_exit: int
) -> None:
    """Catches SSH/signal exit paths that leave an uncommitted candidate running."""
    lab_root = tmp_path / "miloco-lab"
    recovery_log = tmp_path / f"recovery-{recovery_result}.log"
    source = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8")
    source = source.rsplit('\nmain "$@"', maxsplit=1)[0]
    source = source.replace('readonly LAB_ROOT="/opt/miloco-lab"', f'readonly LAB_ROOT="{lab_root}"')
    harness = tmp_path / f"trap-harness-{recovery_result}.sh"
    harness.write_text(
        source
        + "\nrestore_previous() {\n"
        + f"  printf 'restore %s %s\\n' \"$1\" \"$2\" >> '{recovery_log}'\n"
        + f"  return {recovery_result}\n"
        + "}\n"
        + "remove_candidate() { return 99; }\n"
        + 'arm_transition "ai-lab01.esxi" "' + "2" * 40 + '" "' + "1" * 40 + '"\n'
        + "kill -TERM $$\n",
        encoding="utf-8",
    )
    harness.chmod(0o755)
    result = subprocess.run(
        [str(harness)],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == expected_exit
    assert recovery_log.read_text(encoding="utf-8").startswith("restore ai-lab01.esxi")
    if recovery_result:
        assert "rollback_failed" in result.stderr
    else:
        assert "rollback_failed" not in result.stderr


def test_candidate_removal_fails_closed_when_absence_cannot_be_verified(tmp_path: Path) -> None:
    """Catches a failed Compose query being misreported as a removed candidate."""
    source = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8").rsplit(
        '\nmain "$@"', maxsplit=1
    )[0]
    harness = tmp_path / "stop-query-failure.sh"
    harness.write_text(
        source
        + "\ncompose_command() {\n"
        + "  case \"$*\" in *' rm --stop --force '*) return 0 ;; *' ps --all -q '*) return 17 ;; esac\n"
        + "}\n"
        + 'remove_candidate "ai-lab01.esxi" "' + "a" * 40 + '"\n',
        encoding="utf-8",
    )
    harness.chmod(0o755)
    result = subprocess.run(
        [str(harness)], text=True, capture_output=True, check=False, timeout=5
    )
    assert result.returncode != 0


def test_restore_compensation_cannot_continue_after_failed_restart(tmp_path: Path) -> None:
    """Catches trap-context errexit suppression committing state after restore failed."""
    source = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8").rsplit(
        '\nmain "$@"', maxsplit=1
    )[0]
    state_log = tmp_path / "state-write.log"
    harness = tmp_path / "restore-failure.sh"
    harness.write_text(
        source
        + "\nrelease_capability() { printf 'capable\\n'; }\n"
        + "compose_up() { return 17; }\n"
        + "wait_for_health() { return 0; }\n"
        + f"atomic_write() {{ printf 'state-write\\n' > '{state_log}'; }}\n"
        + "set +e\n"
        + 'restore_previous "ai-lab01.esxi" "' + "b" * 40 + '"\n'
        + "restore_status=$?\n"
        + "set -e\n"
        + "[[ \"$restore_status\" -ne 0 ]]\n"
        + f"[[ ! -e '{state_log}' ]]\n",
        encoding="utf-8",
    )
    harness.chmod(0o755)
    result = subprocess.run(
        [str(harness)], text=True, capture_output=True, check=False, timeout=5
    )
    assert result.returncode == 0, result.stderr
