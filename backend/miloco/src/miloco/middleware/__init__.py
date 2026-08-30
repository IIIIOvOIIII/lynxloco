# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Middleware module
"""

from miloco.middleware.exceptions import (
    AuthenticationException,
    AuthorizationException,
    BadRequestException,
    BaseAPIException,
    BusinessException,
    ConflictException,
    ExternalServiceException,
    HaServiceException,
    LLMServiceException,
    MiotOAuthException,
    MiotServiceException,
    ResourceNotFoundException,
    ValidationException,
)

__all__ = [
    "BaseAPIException",
    "BusinessException",
    "ConflictException",
    "ResourceNotFoundException",
    "ExternalServiceException",
    "LLMServiceException",
    "MiotServiceException",
    "MiotOAuthException",
    "HaServiceException",
    "AuthenticationException",
    "AuthorizationException",
    "ValidationException",
    "BadRequestException",
    # Authentication middleware functions
    "verify_service_token",
    "verify_token",
    "verify_token_query_fallback",
    "verify_websocket_token",
]


def __getattr__(name: str):
    if name in {
        "verify_service_token",
        "verify_token",
        "verify_token_query_fallback",
        "verify_websocket_token",
    }:
        from miloco.middleware import auth_middleware

        return getattr(auth_middleware, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
