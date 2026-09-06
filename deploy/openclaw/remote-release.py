#!/usr/bin/env python3
"""Digest-bound native release controller for the registered root OpenClaw profile."""

from __future__ import annotations

import configparser
from contextlib import closing, contextmanager
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile


PROFILE = "openclaw-root-v1"
ALLOWLIST_SHA256 = "02852c989db9f4efd27d1df7e3872f60af982175eb7e1e9f4bf9b751f1754ddd"
MAX_ARCHIVE_BYTES = 2 * 1024**3
MAX_EXPANDED_BYTES = 4 * 1024**3
MIN_FREE_BYTES = 5 * 1024**3
V5_COLUMNS = {
    "traces": {
        "omni_wall_ms": "REAL", "omni_request_count": "INTEGER",
        "omni_request_error_count": "INTEGER", "partial_windows_total": "INTEGER",
        "metric_version": "INTEGER",
    },
    "traces_device": {"partial_windows_count": "INTEGER"},
}
ALLOWLIST = (
    "Dockerfile", "compose.yaml", "container-entrypoint.sh", "remote-release.sh",
    "requirements/backend.txt", "requirements/cli.txt", "requirements/acceptance.txt",
    "wheels/miloco-*.whl", "wheels/miloco_cli-*.whl",
    "wheels/miloco_miot-*-manylinux_2_28_x86_64.whl", "models/miloco-models-*.tar.gz",
    "release.json", "SHA256SUMS",
)
FORBIDDEN = {".git", ".env", "config.json", "credentials.json", ".venv", "venv",
             "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "site-packages"}
FIXTURES = {"h264_annexb_packets.bin", "h264_avcc_packets.bin", "h264_video_audio.mkv", "h265_video_only.mkv"}


class ReleaseError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise ReleaseError(message)


def digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else _digest_stream(handle)


def _digest_stream(handle):
    result = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        result.update(chunk)
    return result.hexdigest()


def tree_digest(directory):
    result = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        relative = str(path.relative_to(directory))
        if path.is_symlink():
            value = "link:" + os.readlink(path)
        elif path.is_file():
            value = "file:" + digest(path)
        else:
            continue
        result.update((relative + "\0" + value + "\0").encode())
    return result.hexdigest()


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        os.chmod(temporary, 0o600)
        json.dump(value, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def normalized_member(member):
    name = member.name
    if name.startswith("./"):
        name = name[2:]
    name = name.rstrip("/")
    if name in ("", ".") and member.isdir():
        return ""
    parts = name.split("/")
    require(not name.startswith("/") and all(part not in ("", ".", "..") for part in parts), "unsafe archive path")
    require(not any(part in FORBIDDEN or part.startswith(".env.") for part in parts), "forbidden archive member")
    require(member.isfile() or member.isdir(), "archive links and special files are forbidden")
    require(not any(char in name for char in "\r\n\\\x00"), "invalid archive member name")
    if name.startswith("acceptance/fixtures/rtsp/"):
        require(name.removeprefix("acceptance/fixtures/rtsp/") in FIXTURES, "unregistered acceptance fixture")
    allowed = any(fnmatch.fnmatchcase(name, pattern) for pattern in ALLOWLIST)
    allowed = allowed or name.startswith("acceptance/") or (member.isdir() and name == "acceptance")
    allowed = allowed or (member.isdir() and any(pattern.startswith(name + "/") for pattern in ALLOWLIST))
    require(allowed, "archive member is not allowlisted")
    return name


def validate_archive(archive):
    names = set()
    expanded = 0
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle:
            name = normalized_member(member)
            if not name:
                continue
            require(name not in names, "duplicate archive member")
            names.add(name)
            expanded += member.size
            require(len(names) <= 20000 and expanded <= MAX_EXPANDED_BYTES, "release archive exceeds bounded size")
    require({"release.json", "SHA256SUMS", "requirements/backend.txt", "requirements/cli.txt"} <= names, "release archive is incomplete")


def extract_archive(archive, destination):
    validate_archive(archive)
    destination.mkdir(mode=0o700)
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle:
            name = normalized_member(member)
            if not name:
                continue
            target = destination / name
            if member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
            else:
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with target.open("xb") as output, bundle.extractfile(member) as source:
                    shutil.copyfileobj(source, output)
                target.chmod(0o600)
    listed = set()
    for line in (destination / "SHA256SUMS").read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        require(match is not None, "invalid artifact checksum record")
        expected, name = match.groups()
        require(name not in listed and name != "SHA256SUMS", "duplicate artifact checksum")
        require(not PurePosixPath(name).is_absolute() and ".." not in PurePosixPath(name).parts, "unsafe checksum path")
        target = destination / name
        require(target.is_file() and digest(target) == expected, "artifact checksum mismatch")
        listed.add(name)
    actual = {str(path.relative_to(destination)) for path in destination.rglob("*") if path.is_file() and path.name != "SHA256SUMS"}
    require(listed == actual, "artifact checksum coverage mismatch")


def compatible_observability_marker(path, prior_version, *, check_only=False):
    """Retain all rows/additive columns; only the known v5 -> v4 marker may change."""
    if not path.exists():
        require(prior_version is None, "observability database is missing")
        return
    mode = "ro" if check_only else "rw"
    with closing(sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True)) as connection:
        current = connection.execute("PRAGMA user_version").fetchone()[0]
        if current == prior_version:
            return
        require(prior_version == 4 and current == 5, "unsupported observability rollback schema")
        for table, columns in V5_COLUMNS.items():
            actual = {row[1]: row[2].upper() for row in connection.execute(f"PRAGMA table_info({table})")}
            require(all(actual.get(name) == kind for name, kind in columns.items()), "observability v5 additive columns do not match")
        if not check_only:
            connection.execute("PRAGMA user_version=4")


class Controller:
    def __init__(self, root=Path("/opt/miloco-openclaw"), home=Path("/root/.openclaw/miloco"),
                 tools=Path("/root/.local/share/uv/tools"), bin_dir=Path("/root/.local/bin")):
        # Path injection exists only for local unit tests; the command line has one fixed profile.
        self.root, self.home, self.tools, self.bin_dir = root, home, tools, bin_dir
        self.python = tools / "miloco/bin/python"
        self.cli = tools / "miloco-cli/bin/miloco-cli"
        self.config = home / "config.json"
        self.supervisor = home / "supervisord.conf"
        self.observability = home / "observability.db"
        self.env = {**os.environ, "MILOCO_HOME": str(home), "PATH": str(bin_dir) + ":" + os.environ.get("PATH", "")}

    def run(self, args, timeout=30):
        try:
            result = subprocess.run([str(arg) for arg in args], env=self.env, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ReleaseError("native command unavailable or timed out") from error
        require(result.returncode == 0, "native command failed (output withheld)")
        return result.stdout.strip()

    def validate_paths(self):
        for path in (self.root, self.home, self.tools, self.bin_dir, self.tools / "miloco", self.tools / "miloco-cli", self.config, self.supervisor):
            for part in (path, *path.parents):
                require(not part.is_symlink(), "native profile contains a symlinked managed path")
                if part.exists():
                    require(part.stat().st_uid == 0 and part.stat().st_mode & 0o022 == 0, "native profile path ownership or permissions mismatch")

    def installed(self):
        backend = json.loads(self.run([self.python, "-c", "import importlib.metadata as m,json,sys; print(json.dumps({'version':m.version('miloco'),'python':sys.executable}))"]))
        cli_version = self.run([self.tools / "miloco-cli/bin/python", "-c", "import importlib.metadata as m; print(m.version('miloco-cli'))"])
        require(backend["python"] == str(self.python), "installed backend interpreter mismatch")
        version = backend["version"]
        short = re.search(r"\+g([0-9a-f]{7,40})(?:\.|$)", version)
        return {"miloco_version": version, "cli_version": cli_version, "python": str(self.python),
                "python_base": str(self.python.resolve()),
                "source_short_commit": short.group(1) if short else None}

    def running_identity(self):
        pid = self.run(["supervisorctl", "-c", self.supervisor, "pid", "miloco-backend"])
        require(pid.isdigit() and int(pid) > 0, "native backend is not running under supervisor")
        process = Path("/proc") / pid
        require(process.stat().st_uid == 0, "native backend service owner mismatch")
        command = (process / "cmdline").read_bytes().split(b"\0")
        require(command[:3] == [str(self.python).encode(), b"-m", b"miloco.main"], "running backend command mismatch")
        environment = (process / "environ").read_bytes().split(b"\0")
        require(f"MILOCO_HOME={self.home}".encode() in environment, "running backend MILOCO_HOME mismatch")
        require((process / "exe").resolve() == self.python.resolve(), "running backend executable mismatch")
        return int(pid)

    def preflight(self, require_running=True):
        require(os.geteuid() == 0, "native operations require root")
        require(platform.system() == "Linux" and platform.machine() == "x86_64", "platform must be linux/amd64")
        self.validate_paths()
        require(self.config.is_file() and self.supervisor.is_file(), "native config or supervisor profile is missing")
        for binary in ("uv", "supervisorctl", "supervisord"):
            require(shutil.which(binary, path=self.env["PATH"]) is not None, "required native tool is unavailable")
        require(self.run(["uv", "tool", "dir"]) == str(self.tools), "uv tool directory mismatch")
        config = json.loads(self.config.read_text())
        server = config.get("server", {})
        require(server.get("python_bin") == str(self.python), "server.python_bin does not match registered native profile")
        require(server.get("port", 1810) == 1810, "native service port mismatch")
        require(config.get("directories", {}).get("storage", ".") in (".", str(self.home)), "native storage must remain in registered home")
        database = Path(config.get("database", {}).get("path", "miloco.db"))
        require(not database.is_absolute() or database.parent == self.home, "database escapes registered native home")
        require(".." not in database.parts, "database escapes registered native home")
        supervisor = configparser.ConfigParser(interpolation=None)
        supervisor.read(self.supervisor)
        section = supervisor["program:miloco-backend"]
        require(shlex.split(section["command"]) == [str(self.python), "-m", "miloco.main"], "supervisor command mismatch")
        require(f'MILOCO_HOME="{self.home}"' in section.get("environment", ""), "supervisor MILOCO_HOME mismatch")
        require(section.get("user", "root") == "root", "supervisor service owner mismatch")
        require(self.observability.is_file(), "registered observability database is missing")
        with closing(sqlite3.connect(f"{self.observability.as_uri()}?mode=ro", uri=True)) as connection:
            require(connection.execute("PRAGMA user_version").fetchone()[0] in (4, 5), "native rollback supports only observability schema v4 or v5")
        required = max(MIN_FREE_BYTES, 2 * sum(path.stat().st_size for folder in (self.tools / "miloco", self.tools / "miloco-cli") for path in folder.rglob("*") if path.is_file()))
        require(shutil.disk_usage(self.home).free >= required, "insufficient disk for native release and rollback")
        require(shutil.disk_usage(self.root if self.root.exists() else self.root.parent).free >= required, "insufficient disk for native backup")
        if require_running:
            self.running_identity()
        return {"runtime_profile": PROFILE, "preflight": "passed", **self.installed()}

    @contextmanager
    def transition(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        with (self.root / "transition.lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ReleaseError("another native transition is active") from error
            yield

    def sqlite_files(self):
        for path in self.home.rglob("*"):
            if path.suffix not in (".db", ".sqlite", ".sqlite3"):
                continue
            require(path.is_file() and not path.is_symlink(), "SQLite backup path is not a regular file")
            with path.open("rb") as handle:
                if handle.read(16) == b"SQLite format 3\0":
                    yield path

    def snapshot_databases(self, destination):
        destination.mkdir(mode=0o700)
        for path in self.sqlite_files():
            target = destination / path.relative_to(self.home)
            target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as source, closing(sqlite3.connect(target)) as backup:
                source.backup(backup)
            target.chmod(0o600)

    def backup(self, sha):
        backup = self.root / "backups" / sha
        require(not backup.exists(), "immutable transaction backup already exists")
        backup.mkdir(mode=0o700, parents=True)
        metadata = {"transaction_sha": sha, "runtime_profile": PROFILE, "before": self.installed(),
                    "config_sha256": digest(self.config), "observability_version": None,
                    "previous_state": json.loads((self.root / "state.json").read_text()) if (self.root / "state.json").exists() else None}
        if self.observability.exists():
            with closing(sqlite3.connect(f"{self.observability.as_uri()}?mode=ro", uri=True)) as connection:
                metadata["observability_version"] = connection.execute("PRAGMA user_version").fetchone()[0]
        for name in ("miloco", "miloco-cli"):
            shutil.copytree(self.tools / name, backup / name, symlinks=True)
        metadata["tool_sha256"] = {name: tree_digest(backup / name) for name in ("miloco", "miloco-cli")}
        shutil.copy2(self.config, backup / "config.json")
        require(digest(backup / "config.json") == metadata["config_sha256"], "configuration changed during backup")
        shutil.copy2(self.supervisor, backup / "supervisord.conf")
        exports = {}
        for name in ("miloco-backend", "miloco-cli"):
            path = self.bin_dir / name
            require(path.is_symlink(), "native entrypoint is not an expected uv tool symlink")
            target = path.resolve()
            require(target.is_relative_to(self.tools), "native entrypoint escapes registered uv tools")
            exports[name] = os.readlink(path)
        metadata["entrypoints"] = exports
        self.snapshot_databases(backup / "sqlite-before")
        atomic_json(backup / "backup.json", metadata)
        return backup, metadata

    def service(self, operation):
        self.run([self.cli, "service", operation], timeout=120)

    def stop(self):
        self.service("stop")
        # CLI stop shuts down its supervisor and reaps the backend; verify the actual process boundary.
        for process in Path("/proc").iterdir():
            if not process.name.isdigit():
                continue
            try:
                command = (process / "cmdline").read_bytes().split(b"\0")
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            require(command[:3] != [str(self.python).encode(), b"-m", b"miloco.main"], "backend still running after stop")

    def health(self):
        deadline = time.monotonic() + 120
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while time.monotonic() < deadline:
            try:
                with opener.open("http://127.0.0.1:1810/health", timeout=5) as response:
                    if response.status == 200:
                        self.running_identity()
                        return
            except (OSError, urllib.error.URLError, ReleaseError):
                pass
            time.sleep(2)
        raise ReleaseError("native health or running-interpreter verification failed")

    def verify(self):
        result = self.preflight()
        self.health()
        state_file = self.root / "state.json"
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
        if state.get("installed"):
            require(result["miloco_version"] == state["installed"]["miloco_version"] and result["cli_version"] == state["installed"]["cli_version"], "installed versions differ from deployed receipt")
        return {**result, "health_http_status": 200, "deployment": state}

    def validate_release(self, release, sha):
        metadata = json.loads((release / "release.json").read_text())
        require(metadata.get("git_sha") == sha and metadata.get("platform") == "linux/amd64", "artifact release identity mismatch")
        versions = {}
        for key, distribution in (("miloco", "miloco"), ("cli", "miloco_cli"), ("miot", "miloco_miot")):
            filename = metadata["artifacts"][key]
            require(Path(filename).name == filename, "invalid wheel artifact name")
            path = release / "wheels" / filename
            require(path.is_file(), "release wheel is missing")
            with zipfile.ZipFile(path) as wheel:
                manifests = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
                require(len(manifests) == 1, "wheel metadata is ambiguous")
                text = wheel.read(manifests[0]).decode()
                name = re.search(r"(?m)^Name: (.+)$", text)
                version = re.search(r"(?m)^Version: (.+)$", text)
                require(name is not None and name.group(1).replace("-", "_") == distribution and version is not None, "wheel metadata identity mismatch")
                versions[key] = version.group(1)
                if key in ("miloco", "cli"):
                    commit = re.search(r"\+g([0-9a-f]{7,40})(?:\.|$)", versions[key])
                    require(commit is not None and sha.startswith(commit.group(1)), "wheel version is not bound to release SHA")
        require(versions["miloco"] == versions["cli"], "backend and CLI release versions differ")
        return metadata, versions

    def restore(self, backup, metadata):
        require(digest(backup / "config.json") == metadata["config_sha256"], "backup configuration digest mismatch")
        for name, expected in metadata["tool_sha256"].items():
            require(name in ("miloco", "miloco-cli") and tree_digest(backup / name) == expected, "backup tool digest mismatch")
        compatible_observability_marker(self.observability, metadata["observability_version"])
        # Preserve the failed installation and every current SQLite file; never copy old DB files back.
        retained = backup / ("superseded-tools-" + str(time.time_ns()))
        retained.mkdir(mode=0o700)
        for name in ("miloco", "miloco-cli"):
            if (self.tools / name).exists():
                os.rename(self.tools / name, retained / name)
            shutil.copytree(backup / name, self.tools / name, symlinks=True)
        for name in ("config.json", "supervisord.conf"):
            temporary = self.home / (".restore-" + name)
            shutil.copy2(backup / name, temporary)
            os.replace(temporary, self.home / name)
        for name, target in metadata["entrypoints"].items():
            temporary = self.bin_dir / (".restore-" + name)
            temporary.symlink_to(target)
            os.replace(temporary, self.bin_dir / name)
        require(self.installed() == metadata["before"], "restored native package versions differ from backup")
        self.service("restart")
        self.health()
        require(digest(self.config) == metadata["config_sha256"], "restored configuration changed on restart")

    def transaction(self, sha, archive_digest, controller_digest, allowlist_digest, stream):
        self.preflight()
        require(re.fullmatch(r"[0-9a-f]{40}", sha), "invalid transaction SHA")
        require(all(re.fullmatch(r"[0-9a-f]{64}", value) for value in (archive_digest, controller_digest, allowlist_digest)), "invalid release digest")
        require(allowlist_digest == ALLOWLIST_SHA256, "artifact allowlist digest mismatch")
        require(digest(Path(__file__)) == controller_digest, "native controller digest mismatch")
        with self.transition():
            release = self.root / "releases" / sha
            require(not release.exists() and not (self.root / "backups" / sha).exists(), "native transaction already exists")
            release.parent.mkdir(mode=0o700, exist_ok=True)
            archive = release.parent / (sha + ".tar.gz")
            with archive.open("xb") as output:
                total = 0
                while chunk := stream.read(1024 * 1024):
                    total += len(chunk)
                    require(total <= MAX_ARCHIVE_BYTES, "release archive exceeds transfer bound")
                    output.write(chunk)
            require(digest(archive) == archive_digest, "release archive digest mismatch")
            extract_archive(archive, release)
            manifest, versions = self.validate_release(release, sha)
            self.preflight()
            backup, metadata = self.backup(sha)
            backend_python = str(self.python.resolve())
            cli_python = str((self.tools / "miloco-cli/bin/python").resolve())
            atomic_json(backup / "release-receipt.json", {"git_sha": sha, "archive_sha256": archive_digest,
                        "controller_sha256": controller_digest, "allowlist_sha256": allowlist_digest, "runtime_profile": PROFILE})
            require(digest(self.config) == metadata["config_sha256"], "configuration changed before service stop")
            try:
                # Stop with the old CLI before replacing either tool environment.
                self.stop()
                wheels = release / "wheels"
                self.run(["uv", "tool", "install", wheels / manifest["artifacts"]["miloco"], "--with", wheels / manifest["artifacts"]["miot"], "--force", "--reinstall", "--python", backend_python, "--no-python-downloads", "--constraints", release / "requirements/backend.txt"], timeout=900)
                self.run(["uv", "tool", "install", wheels / manifest["artifacts"]["cli"], "--force", "--reinstall", "--python", cli_python, "--no-python-downloads", "--constraints", release / "requirements/cli.txt"], timeout=600)
                installed = self.installed()
                require(installed["miloco_version"] == versions["miloco"] and installed["cli_version"] == versions["cli"], "installed wheel versions mismatch")
                require(installed["python_base"] == metadata["before"]["python_base"], "native install changed base Python interpreter")
                require(digest(self.config) == metadata["config_sha256"], "native deployment unexpectedly changed config")
                self.service("restart")
                self.health()
                require(digest(self.config) == metadata["config_sha256"], "native restart unexpectedly changed config")
            except Exception as error:
                try:
                    # The new CLI may be unavailable after a failed install: use the unchanged supervisor.
                    if self.supervisor_socket_running():
                        self.run(["supervisorctl", "-c", self.supervisor, "stop", "miloco-backend"])
                    self.restore(backup, metadata)
                    atomic_json(self.root / "state.json", {"status": "auto-rolled-back", "transaction_sha": sha, "installed": metadata["before"]})
                except Exception as recovery:
                    raise ReleaseError("deployment failed and rollback requires operator action; backup retained") from recovery
                raise ReleaseError("deployment failed; prior native tools and config restored") from error
            state = {"status": "deployed", "transaction_sha": sha, "runtime_profile": PROFILE,
                     "archive_sha256": archive_digest, "controller_sha256": controller_digest, "installed": installed}
            atomic_json(self.root / "state.json", state)
            return {**state, "health_http_status": 200, "config_preserved": True}

    def supervisor_socket_running(self):
        return (self.home / "supervisor.sock").exists()

    def rollback(self, sha):
        self.preflight(require_running=False)
        require(re.fullmatch(r"[0-9a-f]{40}", sha), "invalid rollback transaction SHA")
        with self.transition():
            state = json.loads((self.root / "state.json").read_text())
            require(state.get("status") == "deployed" and state.get("transaction_sha") == sha, "rollback must name the current deployed transaction SHA")
            backup = self.root / "backups" / sha
            metadata = json.loads((backup / "backup.json").read_text())
            require(metadata["transaction_sha"] == sha and metadata["runtime_profile"] == PROFILE, "rollback backup identity mismatch")
            require(digest(backup / "config.json") == metadata["config_sha256"], "backup configuration digest mismatch")
            compatible_observability_marker(self.observability, metadata["observability_version"], check_only=True)
            self.snapshot_databases(backup / ("sqlite-at-rollback-" + str(time.time_ns())))
            self.stop()
            self.restore(backup, metadata)
            restored = {"status": "rolled-back", "transaction_sha": sha, "installed": metadata["before"], "runtime_profile": PROFILE}
            atomic_json(self.root / "state.json", restored)
            return {**restored, "health_http_status": 200, "observability_records_preserved": True}


def main(argv):
    require(len(argv) >= 2, "operation and registered host are required")
    operation, host, *args = argv
    approved_host = os.environ.get("MILOCO_DEPLOY_PRODUCTION_HOST", "miloco-production.example.com")
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}", approved_host) and ".." not in approved_host and not approved_host.endswith("."), "invalid production host profile")
    require(host == approved_host, "OpenClaw requires the production host profile")
    os.umask(0o077)
    controller = Controller()
    if operation == "preflight" and not args:
        result = controller.preflight()
    elif operation in ("verify", "status") and not args:
        result = controller.verify()
    elif operation == "transaction" and len(args) == 4:
        result = controller.transaction(*args, sys.stdin.buffer)
    elif operation == "rollback" and len(args) == 1:
        result = controller.rollback(args[0])
    else:
        raise ReleaseError("invalid native operation or arguments")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (ReleaseError, OSError, ValueError, KeyError, tarfile.TarError, zipfile.BadZipFile, sqlite3.Error, configparser.Error) as error:
        # Do not echo config, package-manager output, URLs, or credentials.
        message = str(error) if isinstance(error, ReleaseError) else type(error).__name__
        print("[openclaw-release] ERROR: " + message, file=sys.stderr)
        sys.exit(4)
