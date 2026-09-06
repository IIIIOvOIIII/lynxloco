"""Native release contract; all filesystem and command effects stay in fixtures."""

from contextlib import contextmanager
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import tarfile
import unittest
import zipfile
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
NATIVE = ROOT / "deploy/openclaw/remote-release.py"
SHA = "a" * 40


@contextmanager
def database(path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def load_controller():
    assert NATIVE.exists(), "native deployment controller is missing"
    spec = importlib.util.spec_from_file_location("native_release", NATIVE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NativeContract(unittest.TestCase):
    def fixture_transaction(self, root, module):
        root = root.resolve()
        controller = module.Controller(root / "control", root / "home", root / "tools", root / "bin")
        controller.home.mkdir()
        controller.bin_dir.mkdir()
        for name, command in (("miloco", "miloco-backend"), ("miloco-cli", "miloco-cli")):
            folder = controller.tools / name
            (folder / "bin").mkdir(parents=True)
            (folder / "version").write_text("old")
            (folder / "bin/python").write_text("fixed-python")
            (folder / "bin" / command).write_text("entrypoint")
            (controller.bin_dir / command).symlink_to(folder / "bin" / command)
        controller.config.write_text('{"model":{"omni":{"max_concurrency":1}}}')
        controller.supervisor.write_text("original supervisor")
        with database(controller.observability) as connection:
            connection.executescript("CREATE TABLE traces(id TEXT); CREATE TABLE traces_device(id TEXT); INSERT INTO traces VALUES('baseline'); PRAGMA user_version=4;")
        source = root / "source"
        (source / "wheels").mkdir(parents=True)
        (source / "requirements").mkdir()
        artifacts = {}
        for key, distribution in (("miloco", "miloco"), ("cli", "miloco_cli"), ("miot", "miloco_miot")):
            version = f"2026.8.6.post1.dev305+g{SHA[:9]}"
            suffix = "manylinux_2_28_x86_64" if key == "miot" else "any"
            filename = f"{distribution}-{version}-py3-none-{suffix}.whl"
            with zipfile.ZipFile(source / "wheels" / filename, "w") as wheel:
                wheel.writestr(f"{distribution}-{version}.dist-info/METADATA", f"Name: {distribution}\nVersion: {version}\n")
            artifacts[key] = filename
        for name in ("backend", "cli"):
            (source / "requirements" / (name + ".txt")).write_text("example==1\n")
        (source / "release.json").write_text(json.dumps({"git_sha": SHA, "platform": "linux/amd64", "artifacts": artifacts}))
        (source / "SHA256SUMS").write_text("".join(f"{module.digest(path)}  {path.relative_to(source)}\n" for path in sorted(source.rglob("*")) if path.is_file()))
        archive = root / "artifact.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(source, arcname=".")
        return controller, archive, version

    def test_transaction_and_rollback_preserve_latest_evidence(self):
        module = load_controller()
        with TemporaryDirectory() as temporary:
            controller, archive, version = self.fixture_transaction(Path(temporary), module)
            config = controller.config.read_bytes()
            events = []
            def installed():
                return {"miloco_version": (controller.tools / "miloco/version").read_text(),
                        "cli_version": (controller.tools / "miloco-cli/version").read_text(),
                        "python": str(controller.python), "python_base": str(controller.python.resolve()),
                        "source_short_commit": "prior" if (controller.tools / "miloco/version").read_text() == "old" else SHA[:9]}
            def install(args, **kwargs):
                self.assertIn("stopped", events)
                self.assertTrue((controller.root / "backups" / SHA / "miloco/version").exists())
                self.assertIn("--constraints", args)
                self.assertIn("--python", args)
                self.assertIn("--no-python-downloads", args)
                name = "miloco-cli" if Path(args[3]).name.startswith("miloco_cli-") else "miloco"
                (controller.tools / name / "version").write_text(version)
                events.append("install-" + name)
                return ""
            with patch.object(controller, "preflight"), patch.object(controller, "installed", side_effect=installed), patch.object(controller, "run", side_effect=install), patch.object(controller, "stop", side_effect=lambda: events.append("stopped")), patch.object(controller, "service", side_effect=lambda operation: events.append(operation)), patch.object(controller, "health"):
                with archive.open("rb") as stream:
                    result = controller.transaction(SHA, module.digest(archive), module.digest(NATIVE), module.ALLOWLIST_SHA256, stream)
                self.assertEqual(result["status"], "deployed")
                self.assertEqual(controller.config.read_bytes(), config)
                with database(controller.observability) as connection:
                    for table, columns in module.V5_COLUMNS.items():
                        for name, kind in columns.items():
                            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
                    connection.execute("INSERT INTO traces(id) VALUES('new-live-evidence')")
                    connection.execute("PRAGMA user_version=5")
                controller.config.write_text('{"model":{"omni":{"max_concurrency":8}}}')
                result = controller.rollback(SHA)
                self.assertEqual(result["status"], "rolled-back")
                self.assertEqual(installed()["miloco_version"], "old")
                self.assertEqual(controller.config.read_bytes(), config)
                with database(controller.observability) as connection:
                    self.assertEqual(connection.execute("SELECT id FROM traces").fetchall(), [("baseline",), ("new-live-evidence",)])
                    self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
                self.assertTrue((controller.root / "backups" / SHA / "sqlite-before/observability.db").exists())
                self.assertEqual(events[:3], ["stopped", "install-miloco", "install-miloco-cli"])

    def test_failed_install_restores_both_old_tools(self):
        module = load_controller()
        with TemporaryDirectory() as temporary:
            controller, archive, version = self.fixture_transaction(Path(temporary), module)
            def installed():
                return {"miloco_version": (controller.tools / "miloco/version").read_text(),
                        "cli_version": (controller.tools / "miloco-cli/version").read_text(),
                        "python_base": str(controller.python.resolve())}
            def fail_install(args, **kwargs):
                (controller.tools / "miloco/version").write_text("partial")
                raise module.ReleaseError("simulated install failure")
            with patch.object(controller, "preflight"), patch.object(controller, "installed", side_effect=installed), patch.object(controller, "run", side_effect=fail_install), patch.object(controller, "stop"), patch.object(controller, "service"), patch.object(controller, "health"):
                with archive.open("rb") as stream, self.assertRaisesRegex(module.ReleaseError, "prior native tools and config restored"):
                    controller.transaction(SHA, module.digest(archive), module.digest(NATIVE), module.ALLOWLIST_SHA256, stream)
                self.assertEqual(installed()["miloco_version"], "old")
                self.assertEqual(installed()["cli_version"], "old")
                self.assertEqual(json.loads((controller.root / "state.json").read_text())["status"], "auto-rolled-back")

    def test_failed_install_can_retain_state_without_automatic_rollback(self):
        module = load_controller()
        with TemporaryDirectory() as temporary:
            controller, archive, version = self.fixture_transaction(Path(temporary), module)
            def installed():
                return {"miloco_version": "old", "cli_version": "old",
                        "python_base": str(controller.python.resolve())}
            def fail_install(args, **kwargs):
                (controller.tools / "miloco/version").write_text("partial")
                raise module.ReleaseError("simulated install failure")
            with patch.object(controller, "preflight"), patch.object(controller, "installed", side_effect=installed), patch.object(controller, "run", side_effect=fail_install), patch.object(controller, "stop"), patch.object(controller, "service"), patch.object(controller, "health"), patch.object(controller, "restore", wraps=controller.restore) as restore:
                with archive.open("rb") as stream, self.assertRaisesRegex(module.ReleaseError, "automatic rollback disabled"):
                    controller.transaction(SHA, module.digest(archive), module.digest(NATIVE), module.ALLOWLIST_SHA256, stream, retain_on_failure=True)
                restore.assert_not_called()
                self.assertEqual((controller.tools / "miloco/version").read_text(), "partial")
                self.assertEqual((controller.root / "backups" / SHA / "miloco/version").read_text(), "old")
                state = json.loads((controller.root / "state.json").read_text())
                self.assertEqual(state["status"], "deployment-failed-retained")
                self.assertFalse(state["rollback_performed"])
                controller.rollback(SHA)
                restore.assert_called_once()
                self.assertEqual((controller.tools / "miloco/version").read_text(), "old")

    def test_transaction_command_forwards_retain_policy(self):
        module = load_controller()
        with patch.dict(os.environ, {"MILOCO_DEPLOY_PRODUCTION_HOST": "miloco-production.example.com"}), patch.object(module, "Controller") as factory, patch("sys.stdout", new_callable=io.StringIO):
            factory.return_value.transaction.return_value = {"status": "deployed"}
            module.main(["transaction", "miloco-production.example.com", SHA, "archive", "controller", "allowlist", "retain"])
            self.assertTrue(factory.return_value.transaction.call_args.kwargs["retain_on_failure"])

    def test_unknown_runtime_fails_before_remote_commands(self):
        result = subprocess.run(
            ["bash", str(ROOT / "deploy.sh"), "preflight", "miloco-production.example.com"],
            env={**os.environ, "MILOCO_DEPLOY_RUNTIME": "misspelled"},
            capture_output=True, text=True,
        )
        self.assertIn("unsupported deployment runtime", result.stderr)

    def test_native_preflight_streams_only_controller_and_propagates_failure(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            definitions = root / "definitions.sh"
            definitions.write_text((ROOT / "deploy.sh").read_text().split('\nparse_arguments "$@"')[0])
            payload = root / "payload"
            calls = root / "calls"
            script = f'''source "{definitions}"
PROJECT_ROOT={shlex.quote(str(ROOT))}
source "$PROJECT_ROOT/deploy/openclaw/local.sh"
operation=preflight
host=miloco-production.example.com
openclaw_assert_clean() {{ :; }}
configure_ssh_identity() {{ ssh_args=(-o BatchMode=yes); }}
ssh() {{ printf '%s\\n' "$*" > "{calls}"; cat > "{payload}"; return 4; }}
openclaw_install_controller() {{ printf 'unexpected mutation' >> "{calls}"; }}
openclaw_dispatch
'''
            result = subprocess.run(["bash"], input=script, text=True, capture_output=True)
            self.assertEqual(result.returncode, 4, result.stderr)
            self.assertEqual(payload.read_bytes(), NATIVE.read_bytes())
            self.assertIn("python3 - preflight miloco-production.example.com", calls.read_text())
            self.assertNotIn("unexpected mutation", calls.read_text())

    def test_openclaw_rejects_staging_host(self):
        result = subprocess.run(
            ["bash", str(ROOT / "deploy.sh"), "preflight", "miloco-staging-a.example.com"],
            env={**os.environ, "MILOCO_DEPLOY_RUNTIME": "openclaw"},
            capture_output=True, text=True,
        )
        self.assertIn("OpenClaw requires the production host profile", result.stderr)

    def test_native_receipt_binds_profile_and_controller(self):
        self.assertTrue((ROOT / "deploy/openclaw/local.sh").exists(), "native routing helper is missing")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in ("deploy.sh", "deploy/ai-lab/remote-release.sh", "deploy/ai-lab/artifact-files.txt", "deploy/openclaw/local.sh", "deploy/openclaw/remote-release.py"):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            output = root / "dist/lab" / SHA
            output.mkdir(parents=True)
            archive = output / f"miloco-lab-{SHA}.tar.gz"
            archive.write_bytes(b"bounded archive")
            definitions = (ROOT / "deploy.sh").read_text().split('\nparse_arguments "$@"')[0]
            definitions_path = root / "definitions.sh"
            definitions_path.write_text(definitions)
            setup = f'source "{definitions_path}"\nPROJECT_ROOT=' + shlex.quote(str(root)) + '\nsource "$PROJECT_ROOT/deploy/openclaw/local.sh"\n'
            setup += f'write_build_receipt {SHA} "{archive}" "{output}/miloco-lab-{SHA}.receipt" "{output}/temporary"\n'
            setup += f'openclaw_write_receipt {SHA}\nopenclaw_read_receipt {SHA}\n'
            result = subprocess.run(["bash"], input=setup, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = output / f"miloco-lab-{SHA}.openclaw.receipt"
            self.assertIn("runtime_profile=openclaw-root-v1", receipt.read_text())
            original_receipt = receipt.read_text()
            receipt.chmod(0o600)
            receipt.write_text(receipt.read_text().replace("openclaw-root-v1", "other-profile"))
            result = subprocess.run(["bash"], input=f'source "{definitions_path}"\nPROJECT_ROOT="{root}"\nsource "$PROJECT_ROOT/deploy/openclaw/local.sh"\nopenclaw_read_receipt {SHA}\n', text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            receipt.write_text(original_receipt)
            controller = root / "deploy/openclaw/remote-release.py"
            controller.write_text(controller.read_text() + "\n# changed after build\n")
            result = subprocess.run(["bash"], input=f'source "{definitions_path}"\nPROJECT_ROOT="{root}"\nsource "$PROJECT_ROOT/deploy/openclaw/local.sh"\nopenclaw_read_receipt {SHA}\n', text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)

    def test_failed_preflight_does_not_create_release_or_backup(self):
        module = load_controller()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller = module.Controller(root / "control", root / "home", root / "tools", root / "bin")
            with patch.object(controller, "preflight", side_effect=module.ReleaseError("bad profile")):
                with self.assertRaises(module.ReleaseError):
                    controller.transaction(SHA, "b" * 64, "c" * 64, module.ALLOWLIST_SHA256, io.BytesIO())
            self.assertEqual(list(root.iterdir()), [])

    def test_archive_rejects_unallowlisted_and_link_members(self):
        module = load_controller()
        for name, link in (("../../config.json", False), ("wheels/miloco-a.whl", True), ("config.json", False)):
            with self.subTest(name=name), TemporaryDirectory() as temporary:
                archive = Path(temporary) / "bad.tar.gz"
                with tarfile.open(archive, "w:gz") as tar:
                    member = tarfile.TarInfo(name)
                    if link:
                        member.type = tarfile.SYMTYPE
                        member.linkname = "/etc/passwd"
                    tar.addfile(member)
                with self.assertRaises(module.ReleaseError):
                    module.validate_archive(archive)

    def test_schema_rollback_preserves_new_records_and_columns(self):
        module = load_controller()
        with TemporaryDirectory() as temporary:
            db = Path(temporary) / "observability.db"
            with database(db) as connection:
                connection.executescript("CREATE TABLE traces(id TEXT); CREATE TABLE traces_device(id TEXT); INSERT INTO traces VALUES ('new-evidence');")
                for table, columns in module.V5_COLUMNS.items():
                    for name, kind in columns.items():
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
                connection.execute("PRAGMA user_version=5")
            module.compatible_observability_marker(db, 4)
            with database(db) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
                self.assertEqual(connection.execute("SELECT id FROM traces").fetchall(), [("new-evidence",)])
                self.assertIn("metric_version", {row[1] for row in connection.execute("PRAGMA table_info(traces)")})

    def test_schema_rollback_refuses_unrecognized_v5_and_future_schema(self):
        module = load_controller()
        for version in (5, 6):
            with self.subTest(version=version), TemporaryDirectory() as temporary:
                db = Path(temporary) / "observability.db"
                with database(db) as connection:
                    connection.execute(f"PRAGMA user_version={version}")
                with self.assertRaises(module.ReleaseError):
                    module.compatible_observability_marker(db, 4)
                with database(db) as connection:
                    self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], version)


if __name__ == "__main__":
    unittest.main()
