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
    assert 'docker top "$container_id" -eo pid' in remote
    assert "listener_pid" in remote
    assert 'remote_release="${REMOTE_RELEASES}/${sha}"' in controller
    assert "tar -xzf -" in controller
    assert "remote-release.sh" in controller
    assert not re.search(r"\b(?:scp|sftp|rclone|oras|skopeo)\b", controller)
    assert not re.search(r"rsync\b[^\n]*\s\.\s", controller)
    assert not re.search(r"tar\b[^\n]*-C\s+\"?\$?PROJECT_ROOT", controller)


def test_remote_checksum_and_acceptance_precede_activation() -> None:
    """Catches building or switching from an unverified/unaccepted release."""
    remote = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8")
    verify_body = remote.split("verify_release()", maxsplit=1)[1].split("\n}", maxsplit=1)[0]
    build_body = remote.split("build_and_activate()", maxsplit=1)[1].split("\n}", maxsplit=1)[0]
    assert 'sha256sum -c "SHA256SUMS"' in verify_body
    assert build_body.index("verify_release") < build_body.index("--target runtime")
    assert build_body.index("--target runtime") < build_body.index("--target acceptance")
    assert build_body.index("--target acceptance") < build_body.index("miloco-lab-acceptance:$sha")
    assert build_body.index("miloco-lab-acceptance:$sha") < build_body.index("activate_release")
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
    assert re.search(
        r"if ! compose_up\b[\s\S]*restore_previous[\s\S]*return 1",
        activate_body,
    )
    assert "logs --tail" in remote
    assert "sanitize_logs" in remote


def test_explicit_rollback_requires_verified_release_and_image_without_state_delete() -> None:
    """Catches rollback to an unknown SHA or destructive state cleanup."""
    remote = REMOTE_RELEASE_SCRIPT.read_text(encoding="utf-8")
    rollback = remote.split("rollback_release()", maxsplit=1)[1]
    assert "verify_release" in rollback
    assert "docker image inspect" in rollback
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
    assert "docker image rm" in retention_body
    activate_body = remote.split("activate_release()", maxsplit=1)[1].split("\n}", maxsplit=1)[0]
    assert activate_body.index('atomic_write "$CURRENT_FILE"') < activate_body.index(
        "retain_rollback_history"
    )


def test_deploy_streams_one_archive_through_stubbed_ssh(tmp_path: Path) -> None:
    """Catches transfer drift to broad copies or non-content-addressed remote paths."""
    sha = "0123456789abcdef0123456789abcdef01234567"
    repository = tmp_path / "repo"
    controller = repository / "deploy.sh"
    remote_dir = repository / "deploy" / "ai-lab"
    remote_dir.mkdir(parents=True)
    shutil.copy2(DEPLOY_SCRIPT, controller)
    shutil.copy2(REMOTE_RELEASE_SCRIPT, remote_dir / "remote-release.sh")
    archive = repository / "dist" / "lab" / sha / f"miloco-lab-{sha}.tar.gz"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"stubbed-release-archive")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_stub = bin_dir / "git"
    git_stub.write_text(
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        "  *status*) exit 0 ;;\n"
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
    transfer_args = (tmp_path / "ssh-args-2").read_text(encoding="utf-8")
    assert f"/opt/miloco-lab/releases/{sha}" in transfer_args
    assert "tar -xzf -" in transfer_args
    assert (tmp_path / "ssh-stdin-2").read_bytes() == archive.read_bytes()
    activation_args = (tmp_path / "ssh-args-3").read_text(encoding="utf-8")
    assert f"bash -s -- activate ai-lab01.esxi {sha}" in activation_args


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
    assert "unknown release" in rollback.stderr
    assert "docker" not in external_log.read_text(encoding="utf-8")
