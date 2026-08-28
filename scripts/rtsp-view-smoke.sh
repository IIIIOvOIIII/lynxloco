#!/usr/bin/env bash
# Local/lab RTSP live-view measurement. Authentication stays in owner-only config.
set +x
set -euo pipefail

if [[ "$#" -ne 2 ]]; then
    echo "usage: rtsp-view-smoke.sh CAMERA_ID BACKEND_URL" >&2
    exit 2
fi

CAMERA_ID="$1"
BACKEND_URL="${2%/}"
DURATION_SEC="${MILOCO_RTSP_VIEW_SMOKE_DURATION_SEC:-30}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ -z "$CAMERA_ID" || ! "$BACKEND_URL" =~ ^https?://[^[:space:]]+$ ]]; then
    echo "camera ID and an http(s) backend URL are required" >&2
    exit 2
fi
if [[ ! "$DURATION_SEC" =~ ^[1-9][0-9]*$ ]]; then
    echo "MILOCO_RTSP_VIEW_SMOKE_DURATION_SEC must be a positive integer" >&2
    exit 2
fi
if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required" >&2
    exit 2
fi

cd "$REPO_ROOT/backend"
exec uv run python - "$CAMERA_ID" "$BACKEND_URL" "$DURATION_SEC" <<'PY'
from __future__ import annotations

import asyncio
import base64
import json
import signal
import ssl
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from miloco.config import get_settings
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed
from websockets.typing import Subprotocol

CAMERA_ID = sys.argv[1]
BACKEND_URL = sys.argv[2]
DURATION_SEC = int(sys.argv[3])
CAMERA_PROTOCOL = Subprotocol("miloco.camera.v1")


class SmokeFailure(RuntimeError):
    def __init__(self, exit_code: int, safe_message: str) -> None:
        self.exit_code = exit_code
        self.safe_message = safe_message
        super().__init__(safe_message)


def _auth_protocol(token: str) -> Subprotocol:
    encoded = base64.urlsafe_b64encode(token.encode()).decode().rstrip("=")
    return Subprotocol(f"miloco.auth.{encoded}")


def _http_json(path: str, token: str, *, verify_tls: bool) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = Request(f"{BACKEND_URL}{path}", headers=headers)
    context = None
    if BACKEND_URL.startswith("https://") and not verify_tls:
        context = ssl._create_unverified_context()
    try:
        with urlopen(request, timeout=5, context=context) as response:
            payload = json.load(response)
    except HTTPError as error:
        raise SmokeFailure(3, f"backend returned HTTP {error.code}") from None
    except (URLError, TimeoutError, json.JSONDecodeError, UnicodeError):
        raise SmokeFailure(3, "backend status request failed") from None
    if not isinstance(payload, dict):
        raise SmokeFailure(3, "backend returned an invalid status payload")
    return payload


def _websocket_url(camera_path: str) -> str:
    parsed = urlsplit(BACKEND_URL)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    prefix = parsed.path.rstrip("/")
    return urlunsplit(
        (scheme, parsed.netloc, f"{prefix}{camera_path}", "", "")
    )


async def _run() -> int:
    settings = get_settings()
    token = settings.server.token
    verify_tls = settings.server.tls_verify
    encoded_camera_id = quote(CAMERA_ID, safe="")
    state_path = f"/api/cameras/{encoded_camera_id}/stream/state"
    resources_path = "/api/monitor/resources"
    ws_path = f"/api/cameras/{encoded_camera_id}/stream"
    before_resources = await asyncio.to_thread(
        _http_json, resources_path, token, verify_tls=verify_tls
    )
    before_cpu = before_resources.get("cpu_pct")
    if not isinstance(before_cpu, int | float):
        raise SmokeFailure(5, "backend process CPU is unavailable")

    stop = asyncio.Event()
    signal_code = 0
    loop = asyncio.get_running_loop()

    def request_stop(code: int) -> None:
        nonlocal signal_code
        signal_code = code
        stop.set()

    for signum, code in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
        try:
            loop.add_signal_handler(signum, request_stop, code)
        except NotImplementedError:
            pass

    protocols = [CAMERA_PROTOCOL, _auth_protocol(token)] if token else None
    started = time.monotonic()
    first_frame_at: float | None = None
    frame_count = 0
    final_state: dict[str, Any] | None = None
    try:
        async with connect(
            _websocket_url(ws_path),
            subprotocols=protocols,
            open_timeout=5,
            close_timeout=2,
            max_queue=2,
        ) as websocket:
            deadline = started + DURATION_SEC
            while not stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    chunk = await asyncio.wait_for(
                        websocket.recv(), timeout=min(remaining, 1.0)
                    )
                except TimeoutError:
                    continue
                except ConnectionClosed:
                    break
                if not isinstance(chunk, bytes) or not chunk:
                    continue
                frame_count += 1
                if first_frame_at is None:
                    first_frame_at = time.monotonic()
            if signal_code:
                return signal_code
            final_state_payload = await asyncio.to_thread(
                _http_json, state_path, token, verify_tls=verify_tls
            )
            candidate = final_state_payload.get("data")
            if isinstance(candidate, dict):
                final_state = candidate
    except SmokeFailure:
        raise
    except Exception:
        raise SmokeFailure(4, "live WebSocket measurement failed") from None

    elapsed = time.monotonic() - started
    if first_frame_at is None or frame_count == 0:
        raise SmokeFailure(4, "live WebSocket produced no binary video frames")
    if final_state is None:
        raise SmokeFailure(3, "backend stream state is unavailable")
    after_resources = await asyncio.to_thread(
        _http_json, resources_path, token, verify_tls=verify_tls
    )
    after_cpu = after_resources.get("cpu_pct")
    if not isinstance(after_cpu, int | float):
        raise SmokeFailure(5, "backend process CPU is unavailable")

    required_state = (
        "viewer_count",
        "mode",
        "queue_depth",
        "dropped_packets",
    )
    if any(field not in final_state for field in required_state):
        raise SmokeFailure(3, "backend stream state is incomplete")
    first_frame_ms = round((first_frame_at - started) * 1000)
    output_fps = frame_count / elapsed
    cpu_delta = float(after_cpu) - float(before_cpu)
    print(f"first_frame_latency_ms={first_frame_ms}")
    print(f"sample_seconds={DURATION_SEC} output_fps={output_fps:.2f}")
    print(f"process_cpu_pct_delta={cpu_delta:.1f}")
    print(
        "viewer_count={} mode={} queue_depth={} queue_drops={}".format(
            final_state["viewer_count"],
            final_state["mode"],
            final_state["queue_depth"],
            final_state["dropped_packets"],
        )
    )
    return 0


try:
    raise SystemExit(asyncio.run(_run()))
except SmokeFailure as error:
    print(error.safe_message, file=sys.stderr)
    raise SystemExit(error.exit_code) from None
PY
