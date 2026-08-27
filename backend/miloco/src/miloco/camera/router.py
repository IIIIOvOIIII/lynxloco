"""Authenticated generic camera management endpoints."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.websockets import WebSocketDisconnect
from pydantic import ValidationError

from miloco.camera.schema import RtspSourceUpsert
from miloco.camera.service import CameraNotFoundError, CameraService, CameraServiceError
from miloco.camera.stream import LiveStreamHub
from miloco.config import get_settings
from miloco.middleware import (
    verify_token,
    verify_token_query_fallback,
    verify_websocket_token,
)
from miloco.middleware.exceptions import AuthenticationException
from miloco.perception.collect.rtsp_probe import RtspSourceError
from miloco.schema.common_schema import NormalResponse

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/cameras",
    tags=["Cameras"],
)

WS_CAMERA_NOT_FOUND = 4404
WS_CAMERA_DISABLED = 4403
WS_CAMERA_UNAVAILABLE = 1013
WS_STREAM_FAILED = 1011

_SAFE_STREAM_REASONS = {
    "camera_not_found",
    "camera_disabled",
    "camera_unavailable",
    "stream_unavailable",
    "transcode_failed",
    "stream_failed",
}


def _get_camera_service() -> CameraService:
    from miloco.manager import get_manager

    return get_manager().camera_service


def _get_live_stream_hub() -> LiveStreamHub:
    from miloco.manager import get_manager

    return get_manager().live_stream_hub


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
LiveStreamHubDependency = Annotated[LiveStreamHub, Depends(_get_live_stream_hub)]
RtspBody = Annotated[RtspSourceUpsert, Depends(_parse_rtsp_upsert)]


def _raise_management_error(error: CameraServiceError | RtspSourceError) -> None:
    status_code = 404 if isinstance(error, CameraNotFoundError) else 409
    raise HTTPException(
        status_code=status_code,
        detail={"code": error.code, "message": error.safe_message},
    ) from None


@router.get("", dependencies=[Depends(verify_token)])
async def list_cameras(service: CameraServiceDependency) -> NormalResponse:
    try:
        cameras = await service.list_cameras()
    except CameraServiceError as error:
        _raise_management_error(error)
    return NormalResponse(
        code=0,
        message="Cameras retrieved successfully",
        data=[camera.model_dump() for camera in cameras],
    )


@router.post("/rtsp/test", dependencies=[Depends(verify_token)])
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


@router.post("/rtsp", dependencies=[Depends(verify_token)])
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


@router.put("/rtsp/{camera_id}", dependencies=[Depends(verify_token)])
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


@router.post("/{camera_id}/enable", dependencies=[Depends(verify_token)])
async def enable_camera(
    camera_id: str, service: CameraServiceDependency
) -> NormalResponse:
    try:
        camera = await service.enable(camera_id)
    except (CameraServiceError, RtspSourceError) as error:
        _raise_management_error(error)
    return NormalResponse(code=0, message="Camera enabled", data=camera.model_dump())


@router.post("/{camera_id}/disable", dependencies=[Depends(verify_token)])
async def disable_camera(
    camera_id: str, service: CameraServiceDependency
) -> NormalResponse:
    try:
        camera = await service.disable(camera_id)
    except (CameraServiceError, RtspSourceError) as error:
        _raise_management_error(error)
    return NormalResponse(code=0, message="Camera disabled", data=camera.model_dump())


@router.delete("/{camera_id}", dependencies=[Depends(verify_token)])
async def delete_camera(
    camera_id: str, service: CameraServiceDependency
) -> NormalResponse:
    try:
        await service.delete(camera_id)
    except (CameraServiceError, RtspSourceError) as error:
        _raise_management_error(error)
    return NormalResponse(code=0, message="Camera deleted", data=None)


def _stream_close_for_error(error: CameraServiceError) -> tuple[int, str]:
    if isinstance(error, CameraNotFoundError) or error.code == "camera_not_found":
        return WS_CAMERA_NOT_FOUND, "camera_not_found"
    if error.code == "camera_disabled":
        return WS_CAMERA_DISABLED, "camera_disabled"
    return WS_CAMERA_UNAVAILABLE, "camera_unavailable"


def _safe_stream_reason(error_code: str | None) -> str:
    if error_code in _SAFE_STREAM_REASONS:
        return error_code
    return "stream_failed"


@router.get(
    "/{camera_id}/watch",
    dependencies=[Depends(verify_token_query_fallback)],
    summary="Unified live camera view",
)
async def camera_watch_page(
    camera_id: str, service: CameraServiceDependency
) -> HTMLResponse:
    try:
        await service.resolve_live_stream(camera_id)
    except CameraServiceError as error:
        _raise_management_error(error)
    static_dir: Path = get_settings().directories.static_dir
    template = (static_dir / "watch.html").read_text(encoding="utf-8")
    return HTMLResponse(template, headers={"Cache-Control": "no-store"})


async def _watch_for_disconnect(websocket: WebSocket) -> None:
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
    except WebSocketDisconnect:
        return


@router.websocket("/{camera_id}/stream")
async def camera_stream_websocket(
    websocket: WebSocket,
    camera_id: str,
    service: CameraServiceDependency,
    hub: LiveStreamHubDependency,
) -> None:
    try:
        verify_websocket_token(websocket)
    except AuthenticationException:
        await websocket.close(code=1008, reason="unauthorized")
        return
    try:
        await service.resolve_live_stream(camera_id)
    except CameraServiceError as error:
        code, reason = _stream_close_for_error(error)
        await websocket.close(code=code, reason=reason)
        return

    stream = hub.subscribe(camera_id)
    sender: asyncio.Task[None] | None = None
    receiver: asyncio.Task[None] | None = None
    close_code = 1000
    close_reason = ""
    accepted = False
    try:
        await websocket.accept()
        accepted = True

        async def send_chunks() -> None:
            async for chunk in stream:
                await websocket.send_bytes(chunk)

        sender = asyncio.create_task(send_chunks())
        receiver = asyncio.create_task(_watch_for_disconnect(websocket))
        done, _pending = await asyncio.wait(
            {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
        )
        if sender in done:
            sender.result()
            state = hub.state(camera_id)
            if state.error_code is not None:
                close_code = WS_STREAM_FAILED
                close_reason = _safe_stream_reason(state.error_code)
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001
        logger.warning("Camera live stream failed (%s)", type(error).__name__)
        close_code = WS_STREAM_FAILED
        close_reason = _safe_stream_reason(hub.state(camera_id).error_code)
    finally:
        for task in (sender, receiver):
            if task is not None and not task.done():
                task.cancel()
        if sender is not None or receiver is not None:
            await asyncio.gather(
                *(task for task in (sender, receiver) if task is not None),
                return_exceptions=True,
            )
        try:
            await stream.aclose()
        except Exception:  # noqa: BLE001
            logger.warning("Camera live stream detach failed")
        if accepted:
            try:
                await websocket.close(code=close_code, reason=close_reason)
            except Exception:  # noqa: BLE001
                pass
