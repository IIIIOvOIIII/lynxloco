"""CLI tests for Home Assistant management commands."""

from __future__ import annotations

from click.testing import CliRunner

from miloco_cli.main import cli


def test_home_assistant_test_reads_token_from_stdin(monkeypatch) -> None:
    posted: dict[str, object] = {}

    def fake_api_post(path, body=None, **kwargs):
        del kwargs
        posted["path"] = path
        posted["data"] = body
        return {"code": 0, "data": {"connected": True}}

    monkeypatch.setattr("miloco_cli.client.api_post", fake_api_post)

    result = CliRunner().invoke(
        cli,
        [
            "home-assistant",
            "test",
            "--url",
            "http://ha.local:8123",
            "--token-stdin",
        ],
        input="secret-token\n",
    )

    assert result.exit_code == 0
    assert posted["path"] == "/api/home-assistant/test"
    assert posted["data"]["token"] == "secret-token"
    assert "secret-token" not in result.output


def test_home_assistant_enable_control_calls_policy_endpoint(monkeypatch) -> None:
    put: dict[str, object] = {}

    def fake_api_put(path, body=None, **kwargs):
        del kwargs
        put["path"] = path
        put["data"] = body
        return {"code": 0, "data": {"entity_id": "light.kitchen"}}

    monkeypatch.setattr("miloco_cli.client.api_put", fake_api_put)

    result = CliRunner().invoke(
        cli,
        ["home-assistant", "enable-control", "light.kitchen"],
    )

    assert result.exit_code == 0
    assert put["path"] == "/api/home-assistant/entities/light.kitchen/policy"
    assert put["data"] == {"included": True, "control_enabled": True}

