# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Home Assistant management API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from miloco.home_assistant.schema import (
    HaErrorCode,
    HomeAssistantConfigUpdate,
    HomeAssistantEntityPolicyUpdate,
    HomeAssistantError,
)
from miloco.manager import get_manager
from miloco.middleware import verify_token
from miloco.middleware.exceptions import HTTPException
from miloco.schema.common_schema import NormalResponse

router = APIRouter(prefix="/home-assistant", tags=["Home Assistant"])
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


@router.get("/config", response_model=NormalResponse)
async def get_config(current_user: str = Depends(verify_token)):
    del current_user
    return NormalResponse(
        code=0,
        message="ok",
        data=manager.home_assistant_service.public_config().model_dump(),
    )


@router.post("/config", response_model=NormalResponse)
async def save_config(
    body: HomeAssistantConfigUpdate,
    current_user: str = Depends(verify_token),
):
    del current_user
    config = manager.home_assistant_service.save_config(body)
    return NormalResponse(code=0, message="ok", data=config.model_dump())


@router.post("/config/test", response_model=NormalResponse)
@router.post("/test", response_model=NormalResponse)
async def test_config(
    body: HomeAssistantConfigUpdate,
    current_user: str = Depends(verify_token),
):
    del current_user
    try:
        result = await manager.home_assistant_service.test_config(
            base_url=body.base_url,
            token=body.token or "",
            verify_tls=body.verify_tls,
        )
    except HomeAssistantError as exc:
        _raise_ha_error(exc)
    return NormalResponse(code=0, message="ok", data=result.model_dump())


@router.get("/status", response_model=NormalResponse)
async def status(current_user: str = Depends(verify_token)):
    del current_user
    result = await manager.home_assistant_service.status()
    return NormalResponse(code=0, message="ok", data=result.model_dump())


@router.get("/entities", response_model=NormalResponse)
async def list_entities(
    current_user: str = Depends(verify_token),
    refresh: bool = Query(False),
):
    del current_user
    try:
        entities = await manager.home_assistant_service.list_entities(refresh=refresh)
    except HomeAssistantError as exc:
        _raise_ha_error(exc)
    return NormalResponse(
        code=0,
        message="ok",
        data=[entity.model_dump() for entity in entities],
    )


@router.put("/entities/{entity_id:path}/policy", response_model=NormalResponse)
async def update_entity_policy(
    entity_id: str,
    body: HomeAssistantEntityPolicyUpdate,
    current_user: str = Depends(verify_token),
):
    del current_user
    view = manager.home_assistant_service.update_entity_policy(
        entity_id,
        body.included,
        body.control_enabled,
    )
    return NormalResponse(code=0, message="ok", data=view.model_dump())
