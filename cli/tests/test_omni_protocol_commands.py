"""Omni admin profile commands keep protocol explicit and credentials out of output."""

from unittest.mock import patch

from click.testing import CliRunner

from miloco_cli.main import cli

STATE = {
    "code": 0,
    "message": "ok",
    "data": {
        "active": {
            "label": "local",
            "model": "vision-local",
            "base_url": "http://127.0.0.1:8000/v1",
            "api_protocol": "openai_responses",
            "protocol_inferred": False,
            "api_key_masked": "",
            "has_key": False,
            "health": {"state": "ok"},
        },
        "profiles": [],
    },
}


def test_create_responses_profile_sends_explicit_protocol_with_blank_key():
    with patch("miloco_cli.client.api_put", return_value=STATE) as api_put:
        result = CliRunner().invoke(
            cli,
            [
                "admin",
                "omni",
                "create",
                "--label",
                "local",
                "--model",
                "vision-local",
                "--base-url",
                "http://127.0.0.1:8000/v1",
                "--api-protocol",
                "openai_responses",
                "--no-activate",
            ],
        )

    assert result.exit_code == 0, result.output
    api_put.assert_called_once_with(
        "/api/admin/omni-config",
        {
            "label": "local",
            "model": "vision-local",
            "base_url": "http://127.0.0.1:8000/v1",
            "api_protocol": "openai_responses",
            "api_key": "",
            "activate": False,
        },
    )


def test_test_profile_sends_protocol_and_never_prints_key():
    response = {
        "code": 0,
        "message": "ok",
        "data": {"ok": True, "code": "ok", "message": "视觉预检通过"},
    }
    secret = "sk-never-print-me"
    with patch("miloco_cli.client.api_post", return_value=response) as api_post:
        result = CliRunner().invoke(
            cli,
            [
                "admin",
                "omni",
                "test",
                "--label",
                "cloud",
                "--model",
                "vision",
                "--base-url",
                "https://provider.example/v1",
                "--api-protocol",
                "openai_chat_completions",
                "--api-key",
                secret,
            ],
        )

    assert result.exit_code == 0, result.output
    assert secret not in result.output
    api_post.assert_called_once_with(
        "/api/admin/omni-config/test",
        {
            "label": "cloud",
            "model": "vision",
            "base_url": "https://provider.example/v1",
            "api_protocol": "openai_chat_completions",
            "api_key": secret,
        },
    )


def test_select_profile_uses_existing_store():
    with patch("miloco_cli.client.api_post", return_value=STATE) as api_post:
        result = CliRunner().invoke(
            cli, ["admin", "omni", "select", "--label", "local"]
        )

    assert result.exit_code == 0, result.output
    api_post.assert_called_once_with(
        "/api/admin/omni-config/activate", {"label": "local"}
    )


def test_protocol_option_rejects_values_outside_exact_enum_without_request():
    with patch("miloco_cli.client.api_put") as api_put:
        result = CliRunner().invoke(
            cli,
            [
                "admin",
                "omni",
                "create",
                "--label",
                "bad",
                "--model",
                "m",
                "--base-url",
                "https://example/v1",
                "--api-protocol",
                "responses",
            ],
        )

    assert result.exit_code == 2
    assert "Invalid value for '--api-protocol'" in result.output
    api_put.assert_not_called()
