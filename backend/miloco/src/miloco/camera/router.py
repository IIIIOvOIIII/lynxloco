"""Authenticated generic camera management endpoints."""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from miloco.camera.schema import RtspSourceUpsert
from miloco.camera.service import CameraNotFoundError, CameraService, CameraServiceError
from miloco.middleware import verify_token
from miloco.perception.collect.rtsp_probe import RtspSourceError
from miloco.schema.common_schema import NormalResponse

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/cameras",
    tags=["Cameras"],
    dependencies=[Depends(verify_token)],
)


def _get_camera_service() -> CameraService:
    from miloco.manager import get_manager

    return get_manager().camera_service


async def _parse_rtsp_upsert(request: Request) -> RtspSourceUpsert:
    """Validate without exposing FastAPI/Pydantic's input-bearing error details."""
    try:
        payload = await request.json()
        return RtspSourceUpsert.model_validate(payload)
    except (ValidationError, ValueError, TypeError):
        logger.warning("RTSP camera request validation failed")
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_camera_request",
                "message": "RTSP camera request is invalid",
            },
        ) from None


CameraServiceDependency = Annotated[CameraService, Depends(_get_camera_service)]
RtspBody = Annotated[RtspSourceUpsert, Depends(_parse_rtsp_upsert)]


def _raise_management_error(error: CameraServiceError | RtspSourceError) -> None:
    status_code = 404 if isinstance(error, CameraNotFoundError) else 409
    raise HTTPException(
        status_code=status_code,
        detail={"code": error.code, "message": error.safe_message},
    ) from None


@router.get("")
async def list_cameras(service: CameraServiceDependency) -> NormalResponse:
    cameras = await service.list_cameras()
    return NormalResponse(
        code=0,
        message="Cameras retrieved successfully",
        data=[camera.model_dump() for camera in cameras],
    )


@router.post("/rtsp/test")
async def test_rtsp_source(
    body: RtspBody, service: CameraServiceDependency
) -> NormalResponse:
    try:
        result = await service.test_rtsp(body)
    except (CameraServiceError, RtspSourceError) as error:
        _raise_management_error(error)
    return NormalResponse(
        code=0, message="RTSP source test succeeded", data=asdict(result)
    )


@router.post("/rtsp")
async def create_rtsp_source(
    body: RtspBody, service: CameraServiceDependency
) -> NormalResponse:
    try:
        camera = await service.create_rtsp(body)
    except (CameraServiceError, RtspSourceError) as error:
        _raise_management_error(error)
    return NormalResponse(
        code=0, message="RTSP source created", data=camera.model_dump()
    )


@router.put("/rtsp/{camera_id}")
async def edit_rtsp_source(
    camera_id: str, body: RtspBody, service: CameraServiceDependency
) -> NormalResponse:
    try:
        camera = await service.edit_rtsp(camera_id, body)
    except (CameraServiceError, RtspSourceError) as error:
        _raise_management_error(error)
    return NormalResponse(
        code=0, message="RTSP source updated", data=camera.model_dump()
    )


@router.post("/{camera_id}/enable")
async def enable_camera(
    camera_id: str, service: CameraServiceDependency
) -> NormalResponse:
    try:
        camera = await service.enable(camera_id)
    except (CameraServiceError, RtspSourceError) as error:
        _raise_management_error(error)
    return NormalResponse(code=0, message="Camera enabled", data=camera.model_dump())


@router.post("/{camera_id}/disable")
async def disable_camera(
    camera_id: str, service: CameraServiceDependency
) -> NormalResponse:
    try:
        camera = await service.disable(camera_id)
    except (CameraServiceError, RtspSourceError) as error:
        _raise_management_error(error)
    return NormalResponse(code=0, message="Camera disabled", data=camera.model_dump())


@router.delete("/{camera_id}")
async def delete_camera(
    camera_id: str, service: CameraServiceDependency
) -> NormalResponse:
    try:
        await service.delete(camera_id)
    except (CameraServiceError, RtspSourceError) as error:
        _raise_management_error(error)
    return NormalResponse(code=0, message="Camera deleted", data=None)
