# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Home Assistant 接入配置契约测试。"""

from __future__ import annotations

import miloco.config.settings as settings_module
import pytest
from miloco.config import reset_settings
from miloco.config.settings import MilocoSettings
from pydantic import ValidationError


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """每个用例独立 $MILOCO_HOME，避免读到真实用户配置。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    reset_settings()
    yield
    reset_settings()


def _home_assistant_settings_cls():
    cls = getattr(settings_module, "HomeAssistantSettings", None)
    assert cls is not None, "HomeAssistantSettings must be defined"
    return cls


def test_home_assistant_settings_defaults_to_disabled() -> None:
    settings = MilocoSettings()

    assert settings.home_assistant.enabled is False
    assert settings.home_assistant.instance_key == "primary"
    assert settings.home_assistant.base_url == ""
    assert settings.home_assistant.token == ""
    assert settings.home_assistant.verify_tls is True
    assert settings.home_assistant.entities == {}


def test_home_assistant_settings_normalizes_trailing_slash() -> None:
    HomeAssistantSettings = _home_assistant_settings_cls()
    ha = HomeAssistantSettings(
        enabled=True,
        base_url="http://ha.lan:8123/",
        token="secret-token",
    )

    assert ha.base_url == "http://ha.lan:8123"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://ha.lan:8123",
        "http:///api",
        "http://ha.lan:8123/#frag",
        "http://ha.lan:8123/api\nstates",
    ],
)
def test_home_assistant_rejects_unsafe_or_incomplete_urls(url: str) -> None:
    HomeAssistantSettings = _home_assistant_settings_cls()
    with pytest.raises(ValidationError):
        HomeAssistantSettings(enabled=True, base_url=url, token="secret-token")


def test_home_assistant_invalid_url_does_not_leak_token() -> None:
    HomeAssistantSettings = _home_assistant_settings_cls()
    with pytest.raises(ValidationError) as exc_info:
        HomeAssistantSettings(
            enabled=True,
            base_url="ftp://ha.lan:8123",
            token="secret-token",
        )

    for rendered_error in (str(exc_info.value), repr(exc_info.value)):
        assert "secret-token" not in rendered_error


@pytest.mark.parametrize(
    "instance_key",
    ["Primary", "primary.instance", "1primary", "primary instance", ""],
)
def test_home_assistant_instance_key_is_conservative(instance_key: str) -> None:
    HomeAssistantSettings = _home_assistant_settings_cls()
    with pytest.raises(ValidationError):
        HomeAssistantSettings(instance_key=instance_key)
