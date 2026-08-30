# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Unified exception handling middleware
Provides exception handling mechanisms:
1. HTTP middleware: Intercepts all HTTP request exceptions
2. WebSocket exception handling: Handles WebSocket connection exceptions
3. Global handler: Handles all types of exceptions
"""

import logging
from typing import Any

from fastapi import Request, status
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from miloco.middleware.exceptions import BaseAPIException
from miloco.schema.common_schema import NormalResponse

logger = logging.getLogger(__name__)


SYSTEM_ERROR_CODE = 9000
_OMIT_VALIDATION_VALUE = object()


def _json_safe_validation_value(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, nested in value.items():
            safe_nested = _json_safe_validation_value(nested)
            if safe_nested is not _OMIT_VALIDATION_VALUE:
                sanitized[str(key)] = safe_nested
        return sanitized
    if isinstance(value, list | tuple):
        sanitized_items = []
        for item in value:
            safe_item = _json_safe_validation_value(item)
            if safe_item is not _OMIT_VALIDATION_VALUE:
                sanitized_items.append(safe_item)
        return sanitized_items
    return _OMIT_VALIDATION_VALUE


def _create_error_response(
    status_code: int, code: int, message: str, data=None
) -> JSONResponse:
    """
    Create unified error response

    Args:
        status_code: HTTP status code
        code: Business error code
        message: Error message
        data: Optional additional data

    Returns:
        JSONResponse: Formatted error response
    """
    response_data = NormalResponse(code=code, message=message, data=data)
    return JSONResponse(status_code=status_code, content=response_data.model_dump())


def _handle_base_api_exception(exc: BaseAPIException) -> JSONResponse:
    """
    Common method for handling BaseAPIException

    Args:
        exc: BaseAPIException exception object

    Returns:
        JSONResponse: Error response
    """
    logger.error(
        "Request failed - %s: %s", type(exc).__name__, exc.message, exc_info=True
    )

    return _create_error_response(
        status_code=exc.http_status, code=exc.code, message=exc.message
    )


def handle_exception(request: Request, exc: Exception) -> JSONResponse:
    """
    Unified exception handling function - handles all exceptions

    This function handles:
    - RequestValidationError (Pydantic validation errors)
    - Custom API exceptions (authentication, authorization, business exceptions, etc.)
    - FastAPI HTTPException
    - Other system-level exceptions

    Args:
        exc: Exception object
        request: FastAPI request object

    Returns:
        JSONResponse: Unified error response
    """
    # 1. Special handling for RequestValidationError (Pydantic validation errors)
    if isinstance(exc, RequestValidationError):
        logger.warning("Request validation failed for %s", request.url.path)
        errors = []
        for error in exc.errors():
            sanitized_error = _json_safe_validation_value(
                {key: value for key, value in error.items() if key != "input"}
            )
            if sanitized_error is not _OMIT_VALIDATION_VALUE:
                errors.append(sanitized_error)
        return _create_error_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code=1002,  # Parameter validation failure error code, consistent with ValidationException
            message="Request parameter validation failed",
            data=errors,
        )

    # 2. Handle other custom API exceptions
    if isinstance(exc, BaseAPIException):
        return _handle_base_api_exception(exc)

    # 3. Handle FastAPI HTTPException (fallback handling)
    if isinstance(exc, FastAPIHTTPException):
        logger.warning("FastAPI HTTP error - %s: %s", exc.status_code, exc.detail)
        return _create_error_response(
            status_code=exc.status_code,
            code=1000,  # General HTTP error code, consistent with HTTPException base class
            message=str(exc.detail),
        )

    # 4. Handle other exceptions (system exceptions) - final fallback
    exc_type = type(exc)
    logger.error(
        "Unhandled system error - %s: %s", exc_type.__name__, str(exc), exc_info=True
    )
    return _create_error_response(
        status_code=500,
        code=SYSTEM_ERROR_CODE,
        message="Internal server error " + exc_type.__name__ + ": " + str(exc),
    )
