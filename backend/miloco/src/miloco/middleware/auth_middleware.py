# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Backward-compatible authentication dependency imports."""

from miloco.auth.dependencies import (
    extract_bearer_token as _extract_bearer_token,
)
from miloco.auth.dependencies import (
    verify_dashboard_or_service_auth as verify_token,
)
from miloco.auth.dependencies import (
    verify_dashboard_or_service_query_fallback as verify_token_query_fallback,
)
from miloco.auth.dependencies import (
    verify_service_token,
)
from miloco.auth.dependencies import (
    verify_websocket_dashboard_or_service as verify_websocket_token,
)

__all__ = [
    "_extract_bearer_token",
    "verify_service_token",
    "verify_token",
    "verify_token_query_fallback",
    "verify_websocket_token",
]
