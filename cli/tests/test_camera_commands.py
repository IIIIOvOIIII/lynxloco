"""Credential-safe camera management CLI contract tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def _assert_secret_absent(result) -> None:
    combined = result.output + repr(result.exception)
    assert SECRET not in combined


def test_camera_list_maps_get_and_prints_json(runner: CliRunner) -> None:
    response = {"code": 0, "message": "ok", "data": [{"id": SOURCE_ID}]}
    with patch("miloco_cli.client.api_get", return_value=response) as api_get:
        result = runner.invoke(cli, ["camera", "list"])

    assert result.exit_code == 0, result.output
    api_get.assert_called_once_with("/api/cameras")
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
    api_post.assert_called_once_with("/api/cameras/rtsp", _upsert_body(password=SECRET))
    assert json.loads(result.output) == SUCCESS
    _assert_secret_absent(result)


def test_rtsp_edit_uses_put_and_blank_password_preserves_stored_value(
    runner: CliRunner,
) -> None:
    with patch("miloco_cli.client.api_put", return_value=SUCCESS) as api_put:
        result = runner.invoke(
            cli,
            ["camera", "rtsp", "edit", SOURCE_ID, *_common_upsert_args()],
        )

    assert result.exit_code == 0, result.output
    api_put.assert_called_once_with(
        f"/api/cameras/rtsp/{SOURCE_ID}", _upsert_body(password="")
    )
    assert json.loads(result.output) == SUCCESS


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
    api_post.assert_called_once_with(path)
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
    api_delete.assert_called_once_with(f"/api/cameras/{SOURCE_ID}")
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

    assert result.exit_code != 0
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


@pytest.mark.parametrize("exit_code", [2, 3])
def test_camera_preserves_network_and_backend_exit_codes(
    runner: CliRunner, exit_code: int
) -> None:
    with patch("miloco_cli.client.api_get", side_effect=SystemExit(exit_code)):
        result = runner.invoke(cli, ["camera", "list"])

    assert result.exit_code == exit_code


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
    assert "authentication_failed" in result.output
    assert "RTSP authentication failed" in result.output
    assert SECRET not in result.output
    assert URI not in result.output
    assert USERNAME not in result.output
    _assert_secret_absent(result)


def test_debug_invocation_redacts_sensitive_camera_arguments(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from miloco_cli import config as config_module
    from miloco_cli import main as main_module

    monkeypatch.setattr(config_module, "load_config", lambda: {"debug": True})
    monkeypatch.setattr(config_module, "miloco_home", lambda: isolated_config)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "miloco-cli",
            "camera",
            "rtsp",
            "add",
            "--uri",
            URI,
            "--username",
            USERNAME,
            "--password",
            SECRET,
        ],
    )

    main_module._debug_log_invocation()

    debug_text = (isolated_config / "log" / "miloco-cli.log").read_text()
    assert SECRET not in debug_text
    assert URI not in debug_text
    assert USERNAME not in debug_text
    assert "[REDACTED]" in debug_text
