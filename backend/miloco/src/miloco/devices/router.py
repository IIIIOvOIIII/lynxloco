# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Unified smart-home device API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from miloco.devices.schema import UnifiedDeviceControlRequest
from miloco.home_assistant.schema import HaErrorCode, HomeAssistantError
from miloco.manager import get_manager
from miloco.middleware import BusinessException, verify_token
from miloco.middleware.exceptions import HTTPException
from miloco.schema.common_schema import NormalResponse

router = APIRouter(prefix="/devices", tags=["Devices"])
manager = get_manager()


def _raise_ha_error(exc: HomeAssistantError) -> None:
    status_code = {
        HaErrorCode.NOT_CONFIGURED: 400,
        HaErrorCode.UNAUTHORIZED: 401,
        HaErrorCode.TIMEOUT: 504,
        HaErrorCode.UNREACHABLE: 503,
        HaErrorCode.INVALID_JSON: 502,
        HaErrorCode.SERVICE_REJECTED: 502,
        HaErrorCode.CONTROL_DISABLED: 400,
        HaErrorCode.UNSUPPORTED_DOMAIN: 400,
    }.get(exc.code, 502)
    raise HTTPException(
        message=f"{exc.code.value}: {str(exc)}",
        status_code=status_code,
        code=3300,
    ) from exc


@router.get("/home", response_model=NormalResponse)
async def home(
    current_user: str = Depends(verify_token),
    refresh: bool = Query(False),
):
    del current_user
    try:
        data = await manager.devices_service.home(refresh=refresh)
    except HomeAssistantError as exc:
        _raise_ha_error(exc)
    return NormalResponse(code=0, message="ok", data=data.model_dump(mode="json"))


@router.get("/{device_id:path}/spec", response_model=NormalResponse)
async def get_spec(
    device_id: str,
    current_user: str = Depends(verify_token),
):
    del current_user
    try:
        data = await manager.devices_service.get_spec(device_id)
    except HomeAssistantError as exc:
        _raise_ha_error(exc)
    return NormalResponse(code=0, message="ok", data=data.model_dump(mode="json"))


@router.post("/{device_id:path}/control", response_model=NormalResponse)
async def control(
    device_id: str,
    request: UnifiedDeviceControlRequest,
    current_user: str = Depends(verify_token),
):
    del current_user
    try:
        result = await manager.devices_service.control(device_id, request)
    except HomeAssistantError as exc:
        _raise_ha_error(exc)
    return NormalResponse(code=0, message="ok", data=result.model_dump(mode="json"))


@router.get("/{device_id:path}/status", response_model=NormalResponse)
async def status(
    device_id: str,
    iid: str | None = None,
    current_user: str = Depends(verify_token),
):
    del current_user
    try:
        iids = [item.strip() for item in iid.split(",")] if iid else None
        data = await manager.devices_service.status(device_id, iids)
    except HomeAssistantError as exc:
        _raise_ha_error(exc)
    return NormalResponse(code=0, message="ok", data=data)


@router.post("/scenes/{scene_id:path}/trigger", response_model=NormalResponse)
async def trigger_scene(
    scene_id: str,
    current_user: str = Depends(verify_token),
):
    del current_user
    try:
        result = await manager.devices_service.trigger_scene(scene_id)
    except HomeAssistantError as exc:
        _raise_ha_error(exc)
    if not result.success:
        raise BusinessException("Scene trigger failed")
    return NormalResponse(code=0, message="ok", data=result.model_dump(mode="json"))
