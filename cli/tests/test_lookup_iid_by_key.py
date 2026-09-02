"""home_info.lookup_iid_by_key tests (PR3 §3.5)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import miloco_cli.home_info as hi_mod
from miloco_cli.home_info import (
    ValidationError,
    lookup_iid_by_key,
    normalize_bool,
)

# ─── normalize_bool ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        (True, True),
        (False, False),
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("on", True),
        ("yes", True),
        (1, True),
        ("false", False),
        ("0", False),
        ("off", False),
        ("no", False),
        (0, False),
        # 其它值原样返回
        ("brightness", "brightness"),
        (42, 42),
    ],
)
def test_normalize_bool(raw, expected):
    assert normalize_bool(raw) == expected


# ─── lookup_iid_by_key ───────────────────────────────────────────────────────


_INFO = {
    "updated_at": datetime.now(UTC).isoformat(),
    "devices": [
        {
            "did": "lamp",
            "name": "灯",
            "spec": {
                "prop.2.1": {
                    "type_name": "on",
                    "service_description": "Light",
                    "format": "bool",
                },
                "prop.2.2": {
                    "type_name": "brightness",
                    "service_description": "Light",
                    "format": "uint8",
                    "value_range": [1, 100, 1],
                },
            },
        },
        {
            "did": "switch3",
            "name": "三联开关",
            "spec": {
                "prop.2.1": {"type_name": "on", "service_description": "Switch 1"},
                "prop.3.1": {"type_name": "on", "service_description": "Switch 2"},
                "prop.4.1": {"type_name": "on", "service_description": "Switch 3"},
            },
        },
        {
            "did": "ghost",
            "name": "旧缓存设备",
            "spec": {
                "prop.2.1": {"format": "bool", "writeable": True, "readable": True},
            },
        },
        {
            "did": "empty",
            "name": "空 spec 设备",
            "spec": {},
        },
        {
            "did": "ha-climate",
            "name": "主卧空调",
            "spec": {
                "hvac_mode": {
                    "iid": "hvac_mode",
                    "type_name": "hvac_mode",
                    "description": "空调模式",
                    "format": "string",
                    "readable": True,
                    "writeable": True,
                },
                "fan_mode": {
                    "iid": "fan_mode",
                    "type_name": "fan_mode",
                    "description": "风速",
                    "format": "string",
                    "readable": True,
                    "writeable": True,
                    "value_list": [
                        {"value": "auto", "description": "auto"},
                        {"value": "low", "description": "low"},
                        {"value": "medium", "description": "medium"},
                        {"value": "high", "description": "high"},
                    ],
                },
            },
        },
    ],
}


@pytest.fixture(autouse=True)
def patch_home_info(monkeypatch):
    monkeypatch.setattr(
        hi_mod, "_fetch", lambda **kwargs: _INFO
    )


def test_lookup_bare_type_name_unique():
    assert lookup_iid_by_key("lamp", "brightness", _INFO) == "prop.2.2"
    assert lookup_iid_by_key("lamp", "on", _INFO) == "prop.2.1"


def test_lookup_ha_climate_underscored_spec_names():
    assert lookup_iid_by_key("ha-climate", "hvac_mode", _INFO) == "hvac_mode"
    assert lookup_iid_by_key("ha-climate", "fan_mode", _INFO) == "fan_mode"


def test_lookup_iid_passthrough():
    """已是 iid 形态应原样返回，不查 home_info。"""
    assert lookup_iid_by_key("lamp", "prop.2.1", _INFO) == "prop.2.1"
    assert lookup_iid_by_key("lamp", "action.5.3", _INFO) == "action.5.3"


def test_lookup_at_desc():
    assert (
        lookup_iid_by_key("switch3", "on@Switch_1", _INFO) == "prop.2.1"
    )
    assert (
        lookup_iid_by_key("switch3", "on@Switch_3", _INFO) == "prop.4.1"
    )


def test_lookup_at_desc_chinese():
    """米家中文 service_description（``左键`` / ``中键``）应能被 ``@desc`` 反查。

    Regression：旧 _KEY_DESC_RE 限制 desc 为 ``[A-Za-z0-9_-]+``，中文字符不匹配，
    落到 unrecognized key 分支报错。
    """
    info = {
        "devices": [
            {
                "did": "switch_zh",
                "spec": {
                    "prop.2.1": {"type_name": "on", "service_description": "左键"},
                    "prop.3.1": {"type_name": "on", "service_description": "中键"},
                    "prop.4.1": {"type_name": "on", "service_description": "右键"},
                },
            }
        ]
    }
    assert lookup_iid_by_key("switch_zh", "on@左键", info) == "prop.2.1"
    assert lookup_iid_by_key("switch_zh", "on@中键", info) == "prop.3.1"
    assert lookup_iid_by_key("switch_zh", "on@右键", info) == "prop.4.1"


def test_lookup_at_sp_form_no_longer_accepted():
    """``type_name@s.p`` 形态已删除，走 ``@desc`` 分支匹配不到 → 报错。
    用户应改用 ``prop.2.1`` 直接寻址。
    """
    with pytest.raises(ValidationError, match="not matched"):
        lookup_iid_by_key("switch3", "on@2.1", _INFO)


def test_lookup_bare_multi_match_shows_iid_candidates():
    with pytest.raises(ValidationError) as ei:
        lookup_iid_by_key("switch3", "on", _INFO)
    msg = str(ei.value)
    assert "matches" in msg
    assert "prop.2.1" in msg
    assert "prop.3.1" in msg or "prop.4.1" in msg


def test_lookup_unknown_key_raises():
    with pytest.raises(ValidationError, match="not found"):
        lookup_iid_by_key("lamp", "nonexistent", _INFO)


def test_lookup_legacy_cache_hint():
    """spec 没有具名条目 → 提示 has no named entries。"""
    with pytest.raises(ValidationError, match="no named entries"):
        lookup_iid_by_key("ghost", "on", _INFO)


def test_lookup_empty_cache_hint():
    with pytest.raises(ValidationError, match="spec empty"):
        lookup_iid_by_key("empty", "on", _INFO)


def test_lookup_at_desc_with_empty_cache_errors():
    """spec 为空时 ``@desc`` 分支无 candidate → 报 not matched。"""
    with pytest.raises(ValidationError, match="not matched"):
        lookup_iid_by_key("empty", "on@2.1", _INFO)


def test_lookup_invalid_format():
    with pytest.raises(ValidationError, match="unrecognized spec_name"):
        lookup_iid_by_key("lamp", "BAD$KEY", _INFO)
