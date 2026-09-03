from unittest.mock import AsyncMock

import pytest
from miloco.devices.schema import DeviceSource, UnifiedActionResult
from miloco.home_assistant.schema import HaErrorCode, HomeAssistantError
from miloco.rule.runner import RuleRunner
from miloco.rule.schema import RuleAction


class _FakeDevicesService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None, object, list[object] | None]] = []

    async def control(self, did, request):
        self.calls.append((did, request.type, request.iid, request.value, request.params))
        return UnifiedActionResult(
            success=True,
            source=DeviceSource.HOME_ASSISTANT,
            did=did,
            message="ok",
        )


class _RejectingDevicesService:
    async def control(self, did, request):
        del did, request
        raise HomeAssistantError(
            HaErrorCode.CONTROL_DISABLED,
            "Miloco control permission is disabled",
        )


def _runner(devices_service) -> RuleRunner:
    return RuleRunner(
        rules=[],
        miot_proxy=AsyncMock(),
        rule_log_repo=AsyncMock(),
        devices_service=devices_service,
    )


def test_old_rule_action_defaults_to_miot() -> None:
    action = RuleAction(did="123", iid="prop.2.1", value=True)

    assert action.source in {None, "miot"}


def test_ha_rule_action_shape_is_accepted() -> None:
    action = RuleAction(
        source="home_assistant",
        did="ha:primary:light.kitchen",
        iid="on",
        value=True,
    )

    assert action.did == "ha:primary:light.kitchen"
    assert action.iid == "on"


@pytest.mark.asyncio
async def test_ha_rule_action_uses_devices_service() -> None:
    devices = _FakeDevicesService()
    result = await _runner(devices)._execute_action(
        "rule-ha",
        RuleAction(
            source="home_assistant",
            did="ha:primary:light.kitchen",
            iid="on",
            value=True,
        ),
    )

    assert result.result is True
    assert devices.calls == [
        ("ha:primary:light.kitchen", "set_property", "on", True, None)
    ]


@pytest.mark.asyncio
async def test_ha_rule_action_reports_control_disabled() -> None:
    result = await _runner(_RejectingDevicesService())._execute_action(
        "rule-ha",
        RuleAction(
            source="home_assistant",
            did="ha:primary:light.kitchen",
            iid="on",
            value=True,
        ),
    )

    assert result.result is False
    assert result.error is not None
    assert "ha_control_disabled" in result.error
