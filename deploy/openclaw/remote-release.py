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
import tempfile
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
    "native/openclaw.tgz", "native/manifest.json",
)
FORBIDDEN = {".git", ".env", "config.json", "credentials.json", ".venv", "venv",
             "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "site-packages"}
FIXTURES = {"h264_annexb_packets.bin", "h264_avcc_packets.bin", "h264_video_audio.mkv", "h265_video_only.mkv"}
PLUGIN_ID = "miloco-openclaw-plugin"
MODEL_FILES = {"det_4C.onnx", "silero_vad.onnx", "bge-small-zh-v1.5-int8.onnx",
               "human_body_reid_v2.onnx", "bge-small-zh-v1.5-tokenizer.json"}


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


def asset_name(member, kind):
    name = member.name.removeprefix("./").rstrip("/")
    if kind == "plugin":
        require(name == "package" or name.startswith("package/"), "plugin archive must use package root")
        name = name.removeprefix("package").removeprefix("/")
    if name in ("", ".") and member.isdir():
        return ""
    parts = name.split("/")
    require(not name.startswith("/") and all(part not in ("", ".", "..") for part in parts), "unsafe asset archive path")
    require(member.isfile() or member.isdir(), "asset archive links or special files are forbidden")
    require(not any(part in FORBIDDEN or part.startswith(".") for part in parts), "forbidden asset archive member")
    if kind == "models":
        require(name in MODEL_FILES and member.isfile(), "unsupported model archive layout")
    else:
        require(name in ("package.json", "openclaw.plugin.json", "README.md", "LICENSE") or
                name.split("/")[0] in ("dist", "skills"), "unsupported plugin archive layout")
    return name


def inspect_asset_archive(archive, kind):
    files, documents, names = {}, {}, set()
    total = 0
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle:
            name = asset_name(member, kind)
            if not name:
                continue
            require(name not in names, "duplicate asset archive member")
            names.add(name)
            total += member.size
            require(len(names) <= 10000 and total <= MAX_EXPANDED_BYTES, "asset archive exceeds bounded size")
            if member.isfile():
                with bundle.extractfile(member) as handle:
                    files[name] = _digest_stream(handle)
                if name in ("package.json", "openclaw.plugin.json"):
                    require(member.size < 1024 * 1024, "plugin manifest exceeds bound")
                    documents[name] = json.loads(bundle.extractfile(member).read())
    if kind == "models":
        require(set(files) == MODEL_FILES, "model archive does not contain the registered model set")
        return {"files": files}
    package = documents.get("package.json", {})
    require(not package.get("devDependencies"), "plugin artifact contains development dependencies")
    require(package.get("name") == PLUGIN_ID and isinstance(package.get("version"), str), "plugin package identity mismatch")
    require(documents.get("openclaw.plugin.json", {}).get("id") == PLUGIN_ID, "plugin manifest identity mismatch")
    require({"dist/index.mjs", "openclaw.plugin.json", "package.json"} <= files.keys(), "plugin runtime bundle is incomplete")
    return {"files": files, "version": package["version"],
            "tools": documents["openclaw.plugin.json"].get("contracts", {}).get("tools", [])}


def extract_asset(archive, destination, kind):
    inspect_asset_archive(archive, kind)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle:
            name = asset_name(member, kind)
            if not name or member.isdir():
                continue
            target = destination / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            require(not target.is_symlink(), "asset destination is a symlink")
            with target.open("wb") as output, bundle.extractfile(member) as source:
                shutil.copyfileobj(source, output)
            target.chmod(0o600)


def prepare_payload(archive, plugin, sha, output):
    """Add native assets to a new payload; leave the Docker archive/allowlist unchanged."""
    require(re.fullmatch(r"[0-9a-f]{40}", sha), "invalid payload SHA")
    require(not output.exists(), "immutable native asset payload already exists")
    with tempfile.TemporaryDirectory() as temporary:
        stage = Path(temporary) / "release"
        extract_archive(archive, stage)
        release = json.loads((stage / "release.json").read_text())
        require(release["git_sha"] == sha, "native payload source SHA mismatch")
        model_name = release["artifacts"]["models"]
        require(Path(model_name).name == model_name, "invalid model artifact name")
        models = stage / "models" / model_name
        (stage / "native").mkdir()
        shutil.copy2(plugin, stage / "native/openclaw.tgz")
        manifest = {"schema": 2, "git_sha": sha, "base_archive_sha256": digest(archive),
                    "models": {"archive": "models/" + model_name, "sha256": digest(models), **inspect_asset_archive(models, "models")},
                    "plugin": {"archive": "native/openclaw.tgz", "sha256": digest(plugin), **inspect_asset_archive(plugin, "plugin")}}
        atomic_json(stage / "native/manifest.json", manifest)
        Controller().validate_release(stage, sha)
        checksums = "".join(f"{digest(path)}  {path.relative_to(stage)}\n" for path in sorted(stage.rglob("*")) if path.is_file() and path.name != "SHA256SUMS")
        (stage / "SHA256SUMS").write_text(checksums)
        with tarfile.open(output, "x:gz") as bundle:
            bundle.add(stage, arcname=".")


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
        self.models = home / "models"
        self.openclaw_home = home.parent
        self.plugin = self.openclaw_home / "extensions" / PLUGIN_ID
        self.openclaw_config = self.openclaw_home / "openclaw.json"
        self.env = {**os.environ, "MILOCO_HOME": str(home), "PATH": str(bin_dir) + ":" + str(self.openclaw_home.parent / ".npm-global/bin") + ":" + os.environ.get("PATH", "")}

    def run(self, args, timeout=30):
        try:
            result = subprocess.run([str(arg) for arg in args], env=self.env, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ReleaseError("native command unavailable or timed out") from error
        require(result.returncode == 0, "native command failed (output withheld)")
        return result.stdout.strip()

    def validate_paths(self):
        for path in (self.root, self.home, self.tools, self.bin_dir, self.tools / "miloco", self.tools / "miloco-cli", self.config, self.supervisor, self.models, self.plugin, self.openclaw_config):
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

    def command_json(self, args, required):
        output = self.run(args, timeout=45)
        require(len(output) <= 2 * 1024**2, "native JSON command output exceeds bound")
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", output):
            try:
                value, _ = decoder.raw_decode(output[match.start():])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and required <= value.keys():
                return value
        raise ReleaseError("native JSON command returned an unsupported shape")

    def gateway_identity(self, plugin_version):
        gateway = self.command_json(["openclaw", "gateway", "status", "--json"], {"service", "rpc"})
        service = gateway["service"]
        require(service.get("loaded") is True and service.get("runtime", {}).get("status") == "running" and gateway["rpc"].get("ok") is True, "OpenClaw gateway is not running or RPC is unavailable")
        plugins = self.command_json(["openclaw", "plugins", "list", "--json"], {"plugins"})
        matches = [entry for entry in plugins["plugins"] if entry.get("id") == PLUGIN_ID]
        require(len(matches) == 1, "registered OpenClaw plugin is missing or ambiguous")
        plugin = matches[0]
        require(plugin.get("status") == "loaded" and plugin.get("enabled") is True and not plugin.get("error"), "OpenClaw plugin did not load")
        require(plugin.get("version") == plugin_version and plugin.get("source") == str(self.plugin / "dist/index.mjs"), "loaded OpenClaw plugin version or source mismatch")
        return {"gateway_rpc_ok": True, "plugin_loaded": True, "plugin_version": plugin_version}

    def capability_flags(self):
        # Same capability probe as scripts/install.py, including partial timeout output.
        try:
            result = subprocess.run(["openclaw", "plugins", "install", "--help"], env=self.env,
                                    capture_output=True, text=True, timeout=30)
            output = result.stdout + "\n" + result.stderr
        except subprocess.TimeoutExpired as error:
            output = "\n".join(value.decode(errors="replace") if isinstance(value, bytes) else value or "" for value in (error.stdout, error.stderr))
            require("--accept-capabilities" in output, "OpenClaw capability probe timed out without a supported flag")
        return ["--accept-capabilities"] if "--accept-capabilities" in output else []

    def asset_preflight(self, require_running=True):
        require(shutil.which("openclaw", path=self.env["PATH"]) is not None, "OpenClaw CLI is missing from registered profile")
        require(self.env.get("OPENCLAW_STATE_DIR", str(self.openclaw_home)) == str(self.openclaw_home), "unsupported OpenClaw state directory override")
        require(self.env.get("OPENCLAW_CONFIG_PATH", str(self.openclaw_config)) == str(self.openclaw_config), "unsupported OpenClaw config path override")
        require(self.env.get("MILOCO_DIRECTORIES__MODELS", str(self.models)) in ("models", str(self.models)), "unsupported model directory environment override")
        config = json.loads(self.config.read_text())
        require(config.get("directories", {}).get("models", "models") in ("", "models", str(self.models)), "unsupported configured model directory")
        require(self.models.is_dir() and all((self.models / name).is_file() and not (self.models / name).is_symlink() for name in MODEL_FILES), "registered model directory is incomplete")
        require(self.plugin.is_dir() and self.openclaw_config.is_file(), "registered OpenClaw plugin or config is missing")
        require(isinstance(json.loads(self.openclaw_config.read_text()), dict), "OpenClaw config must be a JSON object")
        package = json.loads((self.plugin / "package.json").read_text())
        require(package.get("name") == PLUGIN_ID and json.loads((self.plugin / "openclaw.plugin.json").read_text()).get("id") == PLUGIN_ID, "installed OpenClaw plugin identity mismatch")
        self.capability_flags()
        require("--refresh" in self.run(["openclaw", "plugins", "registry", "--help"], timeout=45), "OpenClaw registry refresh is unavailable")
        if require_running:
            self.gateway_identity(package["version"])
        return {"plugin_version": package["version"], "models_sha256": {name: digest(self.models / name) for name in sorted(MODEL_FILES)}}

    def verify_assets(self, assets):
        for key, directory in (("models", self.models), ("plugin", self.plugin)):
            for name, expected in assets[key]["files"].items():
                path = directory / name
                require(path.is_file() and not path.is_symlink() and digest(path) == expected, "installed native asset digest mismatch")
        require(json.loads((self.plugin / "package.json").read_text())["version"] == assets["plugin"]["version"], "installed OpenClaw plugin version mismatch")
        return {"models_verified": True, "plugin_files_verified": True, "plugin_version": assets["plugin"]["version"]}

    def restart_gateway(self, plugin_version):
        self.run(["openclaw", "plugins", "registry", "--refresh"], timeout=120)
        self.run(["openclaw", "gateway", "restart"], timeout=120)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                return self.gateway_identity(plugin_version)
            except ReleaseError:
                time.sleep(2)
        raise ReleaseError("OpenClaw gateway/plugin acceptance timed out")

    def install_assets(self, release, assets):
        # Models follow the upstream installer: overwrite shipped names, retain unrelated files.
        extract_asset(release / assets["models"]["archive"], self.models, "models")
        self.run(["openclaw", "plugins", "install", "--force", *self.capability_flags(), release / assets["plugin"]["archive"]], timeout=600)
        self.verify_assets(assets)

    def preflight(self, require_running=True, recovery=False):
        require(os.geteuid() == 0, "native operations require root")
        require(platform.system() == "Linux" and platform.machine() == "x86_64", "platform must be linux/amd64")
        self.validate_paths()
        require(self.config.is_file() and self.supervisor.is_file(), "native config or supervisor profile is missing")
        if recovery:
            # Failed-retained installation may have missing package files; trust only checked backups.
            return {"runtime_profile": PROFILE, "recovery_preflight": "passed"}
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
        return {"runtime_profile": PROFILE, "preflight": "passed", **self.installed(), **self.asset_preflight(require_running)}

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
        for name, path in (("models", self.models), ("plugin", self.plugin)):
            shutil.copytree(path, backup / name, symlinks=True)
        shutil.copy2(self.openclaw_config, backup / "openclaw.json")
        metadata["assets_before"] = {
            "models_sha256": tree_digest(backup / "models"), "plugin_sha256": tree_digest(backup / "plugin"),
            "openclaw_config_sha256": digest(backup / "openclaw.json"),
            "plugin_version": json.loads((backup / "plugin/package.json").read_text())["version"],
        }
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
        if state.get("assets"):
            result.update(self.verify_assets(state["assets"]))
        return {**result, "health_http_status": 200, "deployment": state}

    def validate_release(self, release, sha):
        require((release / "native/manifest.json").is_file(), "native asset manifest is missing")
        assets = json.loads((release / "native/manifest.json").read_text())
        require(assets.get("schema") == 2 and assets.get("git_sha") == sha, "native asset manifest identity mismatch")
        for key in ("models", "plugin"):
            spec = assets[key]
            require(spec["archive"] == "native/openclaw.tgz" if key == "plugin" else spec["archive"].startswith("models/miloco-models-") and Path(spec["archive"]).suffixes[-2:] == [".tar", ".gz"], "native asset archive path mismatch")
            require(".." not in Path(spec["archive"]).parts, "unsafe native asset path")
            archive = release / spec["archive"]
            require(digest(archive) == spec["sha256"], "native asset archive digest mismatch")
            actual = inspect_asset_archive(archive, key)
            require(all(spec.get(field) == value for field, value in actual.items()), "native asset manifest content mismatch")
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
        # Same PEP -> semver prerelease mapping as scripts/version_normalize.py.
        backend_core, _, backend_local = versions["miloco"].partition("+")
        match = re.fullmatch(r"(\d+\.\d+\.\d+)((?:a|b|rc)\d+)?(?:\.(post\d+))?(?:\.(dev\d+))?", backend_core)
        require(match is not None, "unsupported backend release version")
        identifiers = [part for part in match.groups()[1:] if part]
        expected_plugin = match.group(1) + ("-" + ".".join(identifiers) if identifiers else "")
        plugin_core, _, plugin_local = assets["plugin"]["version"].partition("+")
        require(plugin_core == expected_plugin and (not plugin_local or plugin_local == backend_local), "plugin version does not match backend release")
        metadata["native_assets"] = assets
        return metadata, versions

    def restore(self, backup, metadata):
        require(digest(backup / "config.json") == metadata["config_sha256"], "backup configuration digest mismatch")
        prior_assets = metadata["assets_before"]
        require(digest(backup / "openclaw.json") == prior_assets["openclaw_config_sha256"], "backup OpenClaw config digest mismatch")
        for name in ("models", "plugin"):
            require(tree_digest(backup / name) == prior_assets[name + "_sha256"], "backup native asset digest mismatch")
        for name, expected in metadata["tool_sha256"].items():
            require(name in ("miloco", "miloco-cli") and tree_digest(backup / name) == expected, "backup tool digest mismatch")
        compatible_observability_marker(self.observability, metadata["observability_version"])
        # Preserve the failed installation and every current SQLite file; never copy old DB files back.
        retained = backup / ("superseded-tools-" + str(time.time_ns()))
        retained.mkdir(mode=0o700)
        for name, destination in (("models", self.models), ("plugin", self.plugin)):
            if destination.exists():
                os.rename(destination, retained / name)
            shutil.copytree(backup / name, destination, symlinks=True)
        restored_config = self.openclaw_config.with_name(".restore-openclaw.json")
        shutil.copy2(backup / "openclaw.json", restored_config)
        os.replace(restored_config, self.openclaw_config)
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
        self.restart_gateway(prior_assets["plugin_version"])
        self.service("restart")
        self.health()
        require(digest(self.config) == metadata["config_sha256"], "restored configuration changed on restart")
        require(digest(self.openclaw_config) == prior_assets["openclaw_config_sha256"], "restored OpenClaw configuration changed on restart")

    def transaction(self, sha, archive_digest, controller_digest, allowlist_digest, stream, *, retain_on_failure=False):
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
            require(digest(self.openclaw_config) == metadata["assets_before"]["openclaw_config_sha256"], "OpenClaw configuration changed before service stop")
            try:
                self.run(["openclaw", "gateway", "stop"], timeout=120)
                # Stop with the old CLI before replacing either tool environment.
                self.stop()
                wheels = release / "wheels"
                self.run(["uv", "tool", "install", wheels / manifest["artifacts"]["miloco"], "--with", wheels / manifest["artifacts"]["miot"], "--force", "--reinstall", "--python", backend_python, "--no-python-downloads", "--constraints", release / "requirements/backend.txt"], timeout=900)
                self.run(["uv", "tool", "install", wheels / manifest["artifacts"]["cli"], "--force", "--reinstall", "--python", cli_python, "--no-python-downloads", "--constraints", release / "requirements/cli.txt"], timeout=600)
                installed = self.installed()
                require(installed["miloco_version"] == versions["miloco"] and installed["cli_version"] == versions["cli"], "installed wheel versions mismatch")
                require(installed["python_base"] == metadata["before"]["python_base"], "native install changed base Python interpreter")
                require(digest(self.config) == metadata["config_sha256"], "native deployment unexpectedly changed config")
                self.install_assets(release, manifest["native_assets"])
                self.restart_gateway(manifest["native_assets"]["plugin"]["version"])
                self.service("restart")
                self.health()
                require(digest(self.config) == metadata["config_sha256"], "native restart unexpectedly changed config")
            except Exception as error:
                if retain_on_failure:
                    atomic_json(self.root / "state.json", {
                        "status": "deployment-failed-retained", "transaction_sha": sha,
                        "runtime_profile": PROFILE, "rollback_performed": False,
                        "assets_expected": manifest["native_assets"],
                    })
                    raise ReleaseError("deployment failed; automatic rollback disabled by operator; current state and backup retained") from error
                try:
                    self.run(["openclaw", "gateway", "stop"], timeout=120)
                    # The new CLI may be unavailable after a failed install: use the unchanged supervisor.
                    if self.supervisor_socket_running():
                        self.run(["supervisorctl", "-c", self.supervisor, "stop", "miloco-backend"])
                    self.restore(backup, metadata)
                    atomic_json(self.root / "state.json", {"status": "auto-rolled-back", "transaction_sha": sha, "installed": metadata["before"]})
                except Exception as recovery:
                    raise ReleaseError("deployment failed and rollback requires operator action; backup retained") from recovery
                raise ReleaseError("deployment failed; prior native tools and config restored") from error
            state = {"status": "deployed", "transaction_sha": sha, "runtime_profile": PROFILE,
                     "archive_sha256": archive_digest, "controller_sha256": controller_digest, "installed": installed,
                     "assets": manifest["native_assets"]}
            atomic_json(self.root / "state.json", state)
            return {**state, "health_http_status": 200, "config_preserved": True}

    def supervisor_socket_running(self):
        return (self.home / "supervisor.sock").exists()

    def rollback(self, sha):
        self.preflight(require_running=False, recovery=True)
        require(re.fullmatch(r"[0-9a-f]{40}", sha), "invalid rollback transaction SHA")
        with self.transition():
            state = json.loads((self.root / "state.json").read_text())
            require(state.get("status") in ("deployed", "deployment-failed-retained") and state.get("transaction_sha") == sha, "rollback must name the current deployed transaction SHA")
            backup = self.root / "backups" / sha
            metadata = json.loads((backup / "backup.json").read_text())
            require(metadata["transaction_sha"] == sha and metadata["runtime_profile"] == PROFILE, "rollback backup identity mismatch")
            require("assets_before" in metadata, "legacy native backup requires its recorded historical controller")
            require(digest(backup / "config.json") == metadata["config_sha256"], "backup configuration digest mismatch")
            compatible_observability_marker(self.observability, metadata["observability_version"], check_only=True)
            self.snapshot_databases(backup / ("sqlite-at-rollback-" + str(time.time_ns())))
            self.run(["openclaw", "gateway", "stop"], timeout=120)
            try:
                self.stop()
            except ReleaseError:
                # A partial tool install can make the current CLI unavailable; use the immutable old venv.
                self.run([backup / "miloco-cli/bin/python", "-c", "from miloco_cli.main import main; main()", "service", "stop"], timeout=120)
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
    elif operation == "transaction" and len(args) in (4, 5):
        policy = args[4] if len(args) == 5 else "rollback"
        require(policy in ("rollback", "retain"), "invalid native failure policy")
        result = controller.transaction(*args[:4], sys.stdin.buffer, retain_on_failure=policy == "retain")
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
