"""Credential-safe camera management CLI contract tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from miloco_cli.main import cli

SOURCE_ID = "rtsp:00000000-0000-0000-0000-000000000001"
SECRET = "synthetic-camera-secret"
URI = "rtsp://camera.local/live"
USERNAME = "camera-user"
SUCCESS = {"code": 0, "message": "ok", "data": {"id": SOURCE_ID}}


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_dir = tmp_path / "miloco"
    for key in tuple(__import__("os").environ):
        if key.startswith("MILOCO_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MILOCO_HOME", str(config_dir))
    return config_dir


def _upsert_body(*, password: str = "") -> dict:
    return {
        "name": "Living Room",
        "room_name": "Living Room",
        "uri": URI,
        "username": USERNAME,
        "password": password,
        "transport": "tcp",
        "audio_enabled": True,
    }


def _common_upsert_args() -> list[str]:
    return [
        "--name",
        "Living Room",
        "--room",
        "Living Room",
        "--uri",
        URI,
        "--username",
        USERNAME,
    ]


def _edit_replace_args(
    *, username: str = USERNAME, transport: str = "tcp", audio: str = "--audio"
) -> list[str]:
    return [
        "--name",
        "Living Room",
        "--room",
        "Living Room",
        "--uri",
        URI,
        "--username",
        username,
        "--transport",
        transport,
        audio,
    ]


def _assert_secret_absent(result) -> None:
    combined = result.output + (result.stderr or "") + repr(result.exception)
    assert SECRET not in combined


def test_camera_list_maps_get_and_prints_json(runner: CliRunner) -> None:
    response = {"code": 0, "message": "ok", "data": [{"id": SOURCE_ID}]}
    with patch("miloco_cli.client.api_get", return_value=response) as api_get:
        result = runner.invoke(cli, ["camera", "list"])

    assert result.exit_code == 0, result.output
    api_get.assert_called_once_with("/api/cameras", safe_errors=True)
    assert json.loads(result.output) == response


def test_rtsp_test_reads_password_from_stdin_and_maps_post(runner: CliRunner) -> None:
    with patch("miloco_cli.client.api_post", return_value=SUCCESS) as api_post:
        result = runner.invoke(
            cli,
            [
                "camera",
                "rtsp",
                "test",
                "--uri",
                URI,
                "--username",
                USERNAME,
                "--password-stdin",
            ],
            input=f"{SECRET}\n",
        )

    assert result.exit_code == 0, result.output
    api_post.assert_called_once_with(
        "/api/cameras/rtsp/test",
        {
            "name": "",
            "room_name": "",
            "uri": URI,
            "username": USERNAME,
            "password": SECRET,
            "transport": "tcp",
            "audio_enabled": True,
        },
        safe_errors=True,
        sensitive_values=(SECRET, USERNAME, URI),
    )
    assert json.loads(result.output) == SUCCESS
    _assert_secret_absent(result)


def test_rtsp_add_maps_full_body_without_echoing_password(runner: CliRunner) -> None:
    with patch("miloco_cli.client.api_post", return_value=SUCCESS) as api_post:
        result = runner.invoke(
            cli,
            ["camera", "rtsp", "add", *_common_upsert_args(), "--password-stdin"],
            input=f"{SECRET}\n",
        )

    assert result.exit_code == 0, result.output
    api_post.assert_called_once_with(
        "/api/cameras/rtsp",
        _upsert_body(password=SECRET),
        safe_errors=True,
        sensitive_values=(SECRET, USERNAME, URI),
    )
    assert json.loads(result.output) == SUCCESS
    _assert_secret_absent(result)


def test_rtsp_edit_uses_put_and_blank_password_preserves_stored_value(
    runner: CliRunner,
) -> None:
    with patch("miloco_cli.client.api_put", return_value=SUCCESS) as api_put:
        result = runner.invoke(
            cli,
            ["camera", "rtsp", "edit", SOURCE_ID, *_edit_replace_args()],
        )

    assert result.exit_code == 0, result.output
    api_put.assert_called_once_with(
        f"/api/cameras/rtsp/{SOURCE_ID}",
        _upsert_body(password=""),
        safe_errors=True,
        sensitive_values=(USERNAME, URI),
    )
    assert json.loads(result.output) == SUCCESS


@pytest.mark.parametrize(
    "omitted",
    ["--name", "--room", "--uri", "--username", "--transport", "--audio"],
)
def test_rtsp_edit_requires_every_replace_field_without_sending_request(
    runner: CliRunner, omitted: str
) -> None:
    args = _edit_replace_args()
    index = args.index(omitted)
    del args[index : index + (1 if omitted == "--audio" else 2)]
    with patch("miloco_cli.client.api_put") as api_put:
        result = runner.invoke(cli, ["camera", "rtsp", "edit", SOURCE_ID, *args])

    assert result.exit_code == 1
    api_put.assert_not_called()


def test_rtsp_edit_can_explicitly_clear_username_and_choose_media_fields(
    runner: CliRunner,
) -> None:
    expected = {
        **_upsert_body(password=""),
        "username": "",
        "transport": "udp",
        "audio_enabled": False,
    }
    with patch("miloco_cli.client.api_put", return_value=SUCCESS) as api_put:
        result = runner.invoke(
            cli,
            [
                "camera",
                "rtsp",
                "edit",
                SOURCE_ID,
                *_edit_replace_args(username="", transport="udp", audio="--no-audio"),
            ],
        )

    assert result.exit_code == 0, result.output
    api_put.assert_called_once_with(
        f"/api/cameras/rtsp/{SOURCE_ID}",
        expected,
        safe_errors=True,
        sensitive_values=(URI,),
    )


@pytest.mark.parametrize(
    ("command", "path"),
    [
        ("enable", f"/api/cameras/{SOURCE_ID}/enable"),
        ("disable", f"/api/cameras/{SOURCE_ID}/disable"),
    ],
)
def test_camera_state_commands_map_post(
    runner: CliRunner, command: str, path: str
) -> None:
    with patch("miloco_cli.client.api_post", return_value=SUCCESS) as api_post:
        result = runner.invoke(cli, ["camera", command, SOURCE_ID])

    assert result.exit_code == 0, result.output
    api_post.assert_called_once_with(path, safe_errors=True)
    assert json.loads(result.output) == SUCCESS


def test_camera_delete_requires_yes_without_sending_request(runner: CliRunner) -> None:
    with patch("miloco_cli.client.api_delete") as api_delete:
        result = runner.invoke(cli, ["camera", "delete", SOURCE_ID])

    assert result.exit_code == 1
    api_delete.assert_not_called()
    assert "--yes" in result.output


def test_camera_delete_with_yes_maps_delete(runner: CliRunner) -> None:
    with patch("miloco_cli.client.api_delete", return_value=SUCCESS) as api_delete:
        result = runner.invoke(cli, ["camera", "delete", SOURCE_ID, "--yes"])

    assert result.exit_code == 0, result.output
    api_delete.assert_called_once_with(f"/api/cameras/{SOURCE_ID}", safe_errors=True)
    assert json.loads(result.output) == SUCCESS


@pytest.mark.parametrize("input_text", ["", "\n"])
def test_password_stdin_rejects_eof_or_empty_line_without_request(
    runner: CliRunner, input_text: str
) -> None:
    with patch("miloco_cli.client.api_post") as api_post:
        result = runner.invoke(
            cli,
            ["camera", "rtsp", "add", *_common_upsert_args(), "--password-stdin"],
            input=input_text,
        )

    assert result.exit_code == 1
    api_post.assert_not_called()
    _assert_secret_absent(result)


def test_plaintext_password_option_is_not_accepted_or_echoed(
    runner: CliRunner,
) -> None:
    with patch("miloco_cli.client.api_post") as api_post:
        result = runner.invoke(
            cli,
            [
                "camera",
                "rtsp",
                "add",
                *_common_upsert_args(),
                "--password",
                SECRET,
            ],
        )

    assert result.exit_code == 1
    api_post.assert_not_called()
    _assert_secret_absent(result)


def test_camera_output_redacts_sensitive_fields_even_if_backend_echoes_them(
    runner: CliRunner,
) -> None:
    unsafe_response = {
        "code": 0,
        "data": {
            "password": SECRET,
            "username": USERNAME,
            "uri": URI,
            "video_codec": "h264",
        },
    }
    with patch("miloco_cli.client.api_post", return_value=unsafe_response):
        result = runner.invoke(
            cli,
            ["camera", "rtsp", "add", *_common_upsert_args(), "--password-stdin"],
            input=f"{SECRET}\n",
        )

    assert result.exit_code == 0, result.output
    assert "h264" in result.output
    assert SECRET not in result.output
    assert USERNAME not in result.output
    assert URI not in result.output
    _assert_secret_absent(result)


def test_camera_success_redacts_long_quoted_credentials_in_non_sensitive_fields(
    runner: CliRunner,
) -> None:
    password = 'quote"secret\\tail'
    username = 'user"name\\tail'
    response = {
        "code": 0,
        "message": f"created with {password} for {username}",
        "data": {
            "id": SOURCE_ID,
            "name": "Camera retained",
            "nested": [
                {"password_echo": password},
                {"username_echo": f"owner={username}"},
            ],
        },
    }
    with patch("miloco_cli.client.api_post", return_value=response):
        result = runner.invoke(
            cli,
            [
                "camera",
                "rtsp",
                "add",
                "--name",
                "Camera retained",
                "--room",
                "Hall",
                "--uri",
                URI,
                "--username",
                username,
                "--password-stdin",
                "--pretty",
            ],
            input=f"{password}\n",
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"]["id"] == SOURCE_ID
    assert payload["data"]["name"] == "Camera retained"
    assert payload["message"] == "created with [REDACTED] for [REDACTED]"
    assert payload["data"]["nested"] == [
        {"password_echo": "[REDACTED]"},
        {"username_echo": "owner=[REDACTED]"},
    ]
    assert password not in repr(payload)
    assert username not in repr(payload)


def test_camera_output_redaction_does_not_corrupt_normal_short_value_substrings(
    runner: CliRunner,
) -> None:
    short_uri = "rtsp://c/live"
    response = {
        "code": 0,
        "data": [
            {
                "id": "rtsp:00000000-0000-0000-0000-000000000010",
                "name": "Camera 0",
                "room_name": "Hall",
                "video_codec": "h264",
                "nested": {
                    "password": "0",
                    "username": "a",
                    "uri": short_uri,
                    "password_echo": "0",
                    "username_echo": "a",
                    "message": "Camera 0 assigned a channel",
                    "error_code": "camera_ok",
                },
            }
        ],
    }
    with patch("miloco_cli.client.api_post", return_value=response):
        result = runner.invoke(
            cli,
            [
                "camera",
                "rtsp",
                "add",
                "--name",
                "Camera 0",
                "--room",
                "Hall",
                "--uri",
                short_uri,
                "--username",
                "a",
                "--password-stdin",
                "--pretty",
            ],
            input="0\n",
        )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"][0]
    assert data["id"] == "rtsp:00000000-0000-0000-0000-000000000010"
    assert data["name"] == "Camera 0"
    assert data["room_name"] == "Hall"
    assert data["video_codec"] == "h264"
    assert data["nested"] == {
        "password": "[REDACTED]",
        "username": "[REDACTED]",
        "uri": "[REDACTED]",
        "password_echo": "[REDACTED]",
        "username_echo": "[REDACTED]",
        "message": "Camera 0 assigned a channel",
        "error_code": "camera_ok",
    }


@pytest.mark.parametrize("exit_code", [2, 3])
def test_camera_preserves_network_and_backend_exit_codes(
    runner: CliRunner, exit_code: int
) -> None:
    with patch("miloco_cli.client.api_get", side_effect=SystemExit(exit_code)):
        result = runner.invoke(cli, ["camera", "list"])

    assert result.exit_code == exit_code


def test_camera_abort_preserves_validation_exit_code(runner: CliRunner) -> None:
    with patch("miloco_cli.client.api_get", side_effect=click.Abort()):
        result = runner.invoke(cli, ["camera", "list"])

    assert result.exit_code == 1


def test_unexpected_api_exception_is_safe_and_exits_as_business_failure(
    runner: CliRunner,
) -> None:
    with patch(
        "miloco_cli.client.api_post",
        side_effect=RuntimeError(f"unsafe request contained {SECRET}"),
    ):
        result = runner.invoke(
            cli,
            ["camera", "rtsp", "add", *_common_upsert_args(), "--password-stdin"],
            input=f"{SECRET}\n",
        )

    assert result.exit_code == 3
    assert "camera_request_failed" in result.output
    _assert_secret_absent(result)


def test_backend_stable_error_is_displayed_without_request_material(
    runner: CliRunner,
) -> None:
    response = MagicMock()
    response.is_success = False
    response.status_code = 409
    response.text = "unused"
    response.json.return_value = {
        "detail": {
            "code": "authentication_failed",
            "message": "RTSP authentication failed",
        }
    }
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.post.return_value = response
    with patch("miloco_cli.client._get_client", return_value=client):
        result = runner.invoke(
            cli,
            ["camera", "rtsp", "add", *_common_upsert_args(), "--password-stdin"],
            input=f"{SECRET}\n",
        )

    assert result.exit_code == 3
    assert json.loads(result.output) == {
        "error": {
            "code": "authentication_failed",
            "message": "RTSP authentication failed",
        }
    }
    assert SECRET not in result.output
    assert URI not in result.output
    assert USERNAME not in result.output
    _assert_secret_absent(result)


@pytest.mark.parametrize("invalid_json", [False, True])
def test_backend_unsafe_or_invalid_error_is_generic_without_leaking_request(
    runner: CliRunner, invalid_json: bool
) -> None:
    userinfo_uri = f"rtsp://{USERNAME}:{SECRET}@camera.local/live"
    response = MagicMock()
    response.is_success = False
    response.status_code = 502
    response.text = f"proxy echoed {SECRET} {USERNAME} {userinfo_uri}"
    if invalid_json:
        response.json.side_effect = ValueError(response.text)
    else:
        response.json.return_value = {
            "detail": {
                "code": "proxy_error",
                "message": response.text,
                "request": {"password": SECRET},
            }
        }
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.post.return_value = response
    with patch("miloco_cli.client._get_client", return_value=client):
        result = runner.invoke(
            cli,
            ["camera", "rtsp", "add", *_common_upsert_args(), "--password-stdin"],
            input=f"{SECRET}\n",
        )

    assert result.exit_code == 3
    assert json.loads(result.output) == {
        "error": {
            "code": "camera_request_failed",
            "message": "Camera request failed",
        }
    }
    assert USERNAME not in result.output
    assert userinfo_uri not in result.output
    _assert_secret_absent(result)


@pytest.mark.parametrize(
    "args",
    [
        ["camera", "rtsp", "add", *_common_upsert_args(), "--transport", "sctp"],
        ["camera", "rtsp", "add", *_common_upsert_args(), "--password", SECRET],
        ["camera", "rtsp", "add", *_common_upsert_args(), "--transport"],
        ["camera", "enable"],
        ["camera", "unknown-command"],
    ],
)
def test_all_camera_usage_errors_exit_one(runner: CliRunner, args: list[str]) -> None:
    result = runner.invoke(cli, args)

    assert result.exit_code == 1
    _assert_secret_absent(result)


def test_password_stdin_rejects_tty_without_reading(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    import miloco_cli.commands.camera as camera_module

    stdin = MagicMock()
    stdin.isatty.return_value = True
    monkeypatch.setattr(camera_module, "sys", SimpleNamespace(stdin=stdin))
    with patch("miloco_cli.client.api_post") as api_post:
        result = runner.invoke(
            cli,
            ["camera", "rtsp", "add", *_common_upsert_args(), "--password-stdin"],
        )

    assert result.exit_code == 1
    assert "pipe" in result.output.lower() or "redirect" in result.output.lower()
    stdin.readline.assert_not_called()
    api_post.assert_not_called()


@pytest.mark.parametrize("style", ["separate", "equals"])
def test_debug_invocation_redacts_sensitive_camera_arguments(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch, style: str
) -> None:
    from miloco_cli import config as config_module
    from miloco_cli import main as main_module

    monkeypatch.setattr(config_module, "load_config", lambda: {"debug": True})
    monkeypatch.setattr(config_module, "miloco_home", lambda: isolated_config)
    argv = [
        "miloco-cli",
        "camera",
        "rtsp",
        "add",
    ]
    if style == "separate":
        argv.extend(["--uri", URI, "--username", USERNAME, "--password", SECRET])
    else:
        argv.extend([f"--uri={URI}", f"--username={USERNAME}", f"--password={SECRET}"])
    monkeypatch.setattr(sys, "argv", argv)

    main_module._debug_log_invocation()

    debug_text = (isolated_config / "log" / "miloco-cli.log").read_text()
    assert SECRET not in debug_text
    assert URI not in debug_text
    assert USERNAME not in debug_text
    assert "[REDACTED]" in debug_text
