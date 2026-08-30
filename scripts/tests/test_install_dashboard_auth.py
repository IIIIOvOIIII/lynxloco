"""Fast behavioral tests for the installer dashboard-auth step."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from subprocess import CompletedProcess

import pytest


def _load_installer_module():
    """Load install.py without requiring its optional terminal UI packages."""
    rich_module = types.ModuleType("rich")
    console_module = types.ModuleType("rich.console")
    progress_module = types.ModuleType("rich.progress")
    questionary_module = types.ModuleType("questionary")

    class Placeholder:
        def __init__(self, *args, **kwargs):
            pass

    console_module.Console = Placeholder
    for name in (
        "BarColumn",
        "DownloadColumn",
        "Progress",
        "SpinnerColumn",
        "TextColumn",
        "TimeRemainingColumn",
        "TransferSpeedColumn",
    ):
        setattr(progress_module, name, Placeholder)

    module_name = "miloco_install_for_test"
    previous_modules = {
        name: sys.modules.get(name)
        for name in (
            "rich",
            "rich.console",
            "rich.progress",
            "questionary",
            module_name,
        )
    }
    sys.modules.update(
        {
            "rich": rich_module,
            "rich.console": console_module,
            "rich.progress": progress_module,
            "questionary": questionary_module,
        }
    )
    try:
        module_path = Path(__file__).resolve().parents[1] / "install.py"
        spec = importlib.util.spec_from_file_location("miloco_install_for_test", module_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, previous in previous_modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


@pytest.fixture(scope="module")
def installer_module():
    return _load_installer_module()


class FakeI18n:
    def t(self, key: str, *args: str) -> str:
        return key


class FakeUI:
    def __init__(self, responses: list[str] | None = None) -> None:
        self.i18n = FakeI18n()
        self.responses = iter(responses or [])
        self.events: list[tuple[str, str]] = []

    def phase(self, title: str, subtitle: str = "") -> None:
        self.events.append(("phase", f"{title} {subtitle}"))

    def step_ok(self, message: str, detail: str = "") -> None:
        self.events.append(("ok", f"{message} {detail}"))

    def step_skip(self, message: str) -> None:
        self.events.append(("skip", message))

    def step_fail(self, message: str, hint: str = "") -> None:
        self.events.append(("fail", f"{message} {hint}"))

    def prompt_select(self, message: str, choices: list[str], default=None) -> str:
        return next(self.responses)

    def prompt_input(self, message: str, default="", password=False, validate=None) -> str:
        value = next(self.responses)
        assert validate is None or validate(value) is True
        return value


def _installer(installer_module, tmp_path, interactive: bool, ui: FakeUI):
    platform = installer_module.Platform("test", "test", interactive, "en")
    instance = installer_module.Installer(
        platform,
        ui,
        object(),
        miloco_home=tmp_path,
    )
    instance._service_started = True
    instance._current_step = 1
    instance._total_steps = 1
    return instance


def test_dashboard_auth_skips_existing_administrator(
    installer_module, monkeypatch, tmp_path
) -> None:
    ui = FakeUI()
    installer = _installer(installer_module, tmp_path, True, ui)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return CompletedProcess(
            command,
            0,
            stdout=json.dumps({"code": 0, "data": {"needs_setup": False}}),
        )

    monkeypatch.setattr(installer_module.subprocess, "run", fake_run)

    installer._step_dashboard_auth()

    assert calls == [(["miloco-cli", "auth", "status"], {"check": True, "capture_output": True, "text": True})]
    assert ("ok", "dashboard_auth.already_configured ") in ui.events
    assert not [event for event in ui.events if event[0] == "fail"]


def test_dashboard_auth_interactive_setup_passes_password_only_through_stdin(
    installer_module, monkeypatch, tmp_path, capsys
) -> None:
    password = "installer-secret-not-an-argument"
    csrf_token = "csrf-token-not-an-installer-message"
    ui = FakeUI(
        [
            "dashboard_auth.create_now",
            "lynx",
            "Lynx",
            password,
            password,
        ]
    )
    installer = _installer(installer_module, tmp_path, True, ui)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[-1] == "status":
            return CompletedProcess(
                command,
                0,
                stdout=json.dumps({"code": 0, "data": {"needs_setup": True}}),
            )
        return CompletedProcess(
            command,
            0,
            stdout=json.dumps({"data": {"csrf_token": csrf_token}}),
        )

    monkeypatch.setattr(installer_module.subprocess, "run", fake_run)

    installer._step_dashboard_auth()

    setup_command, setup_kwargs = calls[1]
    assert password not in setup_command
    assert setup_kwargs["input"] == f"{password}\n"
    assert setup_kwargs["capture_output"] is True
    assert setup_kwargs["text"] is True
    emitted = "\n".join(message for _, message in ui.events)
    assert password not in emitted
    assert csrf_token not in emitted
    captured = capsys.readouterr()
    assert password not in captured.out
    assert password not in captured.err
    assert csrf_token not in captured.out
    assert csrf_token not in captured.err
    assert ("ok", "dashboard_auth.created ") in ui.events


def test_dashboard_auth_non_interactive_gives_browser_setup_instruction(
    installer_module, monkeypatch, tmp_path
) -> None:
    ui = FakeUI()
    installer = _installer(installer_module, tmp_path, False, ui)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return CompletedProcess(
            command,
            0,
            stdout=json.dumps({"code": 0, "data": {"needs_setup": True}}),
        )

    monkeypatch.setattr(installer_module.subprocess, "run", fake_run)

    installer._step_dashboard_auth()

    assert calls == [(["miloco-cli", "auth", "status"], {"check": True, "capture_output": True, "text": True})]
    assert ("skip", "dashboard_auth.open_browser_to_setup") in ui.events
    assert not [event for event in ui.events if event[0] == "fail"]
