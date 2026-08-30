"""CLI tests for unified MIoT + Home Assistant device endpoints."""

from __future__ import annotations

from click.testing import CliRunner

from miloco_cli.main import cli


def test_device_list_uses_unified_home(monkeypatch) -> None:
    calls: list[str] = []

    def fake_api_get(path, params=None, *, timeout=None, **kwargs):
        del params, timeout, kwargs
        calls.append(path)
        return {
            "code": 0,
            "data": {
                "home_name": "家",
                "devices": [],
                "scenes": [],
                "areas": [],
            },
        }

    monkeypatch.setattr("miloco_cli.client.api_get", fake_api_get)

    result = CliRunner().invoke(cli, ["device", "list"])

    assert result.exit_code == 0
    assert calls == ["/api/devices/home"]


def test_device_control_uses_unified_control_endpoint(monkeypatch) -> None:
    posted: dict[str, object] = {}
    info = {
        "devices": [
            {
                "did": "ha:primary:light.kitchen",
                "name": "厨房灯",
                "online": True,
                "spec": {
                    "on": {
                        "iid": "on",
                        "type_name": "on",
                        "format": "bool",
                        "readable": True,
                        "writeable": True,
                    }
                },
            }
        ]
    }

    monkeypatch.setattr("miloco_cli.home_info._fetch", lambda **kwargs: info)

    def fake_api_post(path, body=None, **kwargs):
        del kwargs
        posted["path"] = path
        posted["body"] = body
        return {"code": 0, "message": "ok", "data": {"results": [{"code": 0}]}}

    monkeypatch.setattr("miloco_cli.client.api_post", fake_api_post)

    result = CliRunner().invoke(
        cli,
        ["device", "control", "ha:primary:light.kitchen", "on", "true"],
    )

    assert result.exit_code == 0
    assert posted["path"] == "/api/devices/ha%3Aprimary%3Alight.kitchen/control"
    assert posted["body"] == {"type": "set_property", "iid": "on", "value": True}

