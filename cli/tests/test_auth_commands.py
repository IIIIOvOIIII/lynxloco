"""Dashboard-auth CLI command tests."""

import json

from click.testing import CliRunner

from miloco_cli.main import cli


def test_auth_setup_requires_password_stdin() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["auth", "setup", "--username", "lynx"])

    assert result.exit_code != 0
    assert "--password-stdin" in result.output


def test_auth_setup_sends_password_in_body_not_argv(monkeypatch) -> None:
    calls = []

    def fake_post(path, body=None, **kwargs):
        calls.append((path, body, kwargs))
        return {"code": 0, "message": "ok", "data": {"user": {"username": "lynx"}}}

    monkeypatch.setattr("miloco_cli.commands.auth.api_post", fake_post)
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            "auth",
            "setup",
            "--username",
            "lynx",
            "--display-name",
            "Lynx",
            "--password-stdin",
        ],
        input="correct horse battery\n",
    )

    assert result.exit_code == 0
    assert calls[0][0] == "/api/auth/setup"
    assert calls[0][1]["password"] == "correct horse battery"
    assert calls[0][1]["password_confirm"] == "correct horse battery"
    assert "correct horse battery" not in result.output


def test_auth_status_prints_backend_response(monkeypatch) -> None:
    response = {"code": 0, "message": "ok", "data": {"needs_setup": True}}
    monkeypatch.setattr("miloco_cli.commands.auth.api_get", lambda path: response)
    runner = CliRunner()

    result = runner.invoke(cli, ["auth", "status"])

    assert result.exit_code == 0
    assert json.loads(result.output) == response


def test_auth_reset_password_matches_username_case_insensitively(monkeypatch) -> None:
    calls = []

    def fake_get(path):
        assert path == "/api/users"
        return {
            "code": 0,
            "message": "ok",
            "data": {"users": [{"id": "user-123", "username": "Lynx"}]},
        }

    def fake_post(path, body=None, **kwargs):
        calls.append((path, body, kwargs))
        return {"code": 0, "message": "ok", "data": {}}

    monkeypatch.setattr("miloco_cli.commands.auth.api_get", fake_get)
    monkeypatch.setattr("miloco_cli.commands.auth.api_post", fake_post)
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["auth", "reset-password", "--username", "lynx", "--password-stdin"],
        input="new secret password\n",
    )

    assert result.exit_code == 0
    assert calls[0][0] == "/api/users/user-123/password"
    assert calls[0][1] == {
        "password": "new secret password",
        "password_confirm": "new secret password",
    }
    assert calls[0][2]["sensitive_values"] == ("new secret password",)
    assert "new secret password" not in result.output
