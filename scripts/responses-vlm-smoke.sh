#!/usr/bin/env bash

set -eu

if [ -z "${MILOCO_RESPONSES_BASE_URL:-}" ] || [ -z "${MILOCO_RESPONSES_MODEL:-}" ]; then
    exit 2
fi

trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(dirname -- "$script_dir")
cd "$repo_root/backend"

python_bin="$repo_root/backend/.venv/bin/python"
if [ ! -x "$python_bin" ]; then
    exit 2
fi

# Responses empty-key mode is explicit. Never let the generic Omni fallback key
# cross into the independently selected Responses endpoint.
unset MILOCO_MODEL__OMNI__API_KEY

exec "$python_bin" - 2>/dev/null <<'PY'
from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import sys
import time
from urllib.parse import urlsplit


def exit_for_signal(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


signal.signal(signal.SIGHUP, exit_for_signal)
signal.signal(signal.SIGINT, exit_for_signal)
signal.signal(signal.SIGTERM, exit_for_signal)
os.environ.pop("MILOCO_MODEL__OMNI__API_KEY", None)

import numpy as np

from miloco.perception.engine.config import OmniConfig
from miloco.perception.engine.omni.omni_client import call_omni, extract_usage
from miloco.perception.engine.omni.probe import probe_omni
from miloco.perception.engine.omni.prompt_builder import build_prompt
from miloco.perception.engine.omni.provider import get_adapter
from miloco.perception.engine.omni.response_parser import parse_omni_response
from miloco.perception.engine.types import (
    AudioAnalysis,
    AudioType,
    FrameInfo,
    FrameResolution,
    IdentityPacket,
    MotionState,
    OmniContext,
    SelectedFrame,
)


def fail(code: int) -> None:
    raise SystemExit(code)


async def run() -> tuple[int, int, bool, dict[str, int]]:
    base_url = os.environ.get("MILOCO_RESPONSES_BASE_URL", "")
    model = os.environ.get("MILOCO_RESPONSES_MODEL", "")
    api_key = os.environ.get("MILOCO_RESPONSES_API_KEY", "")
    parsed_url = urlsplit(base_url)
    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.netloc
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
    ):
        fail(2)
    if re.fullmatch(r"[A-Za-z0-9._:/-]+", model) is None:
        fail(2)

    preflight = await probe_omni(
        model,
        base_url,
        api_key,
        "openai_responses",
    )
    if preflight.get("ok") is not True:
        fail(3)

    frames = [
        np.full((32, 48, 3), value, dtype=np.uint8)
        for value in (48, 160)
    ]
    packet = IdentityPacket(
        packet_id="responses-smoke",
        room_name="synthetic-lab",
        timestamp=1.0,
        frame_info=FrameInfo(start_timestamp=0.0, end_timestamp=1.0, fps=1),
        targets=[],
        scene_motion=MotionState.DYNAMIC,
        frames=[
            SelectedFrame(
                frame_index=index,
                image=frame,
                resolution=FrameResolution.HIGH,
                crops=[],
            )
            for index, frame in enumerate(frames)
        ],
        all_frames=frames,
        audio_clip=np.zeros(0, dtype=np.int16),
        audio_analysis=AudioAnalysis(
            type=AudioType.SILENCE,
            is_urgent=False,
            energy_level=0.0,
        ),
    )
    adapter = get_adapter("openai_responses", model)
    payload = build_prompt(packet, OmniContext(), media_mode=adapter.media_mode)
    image_count = len(payload.get("images", []))
    if image_count < 1 or image_count > 12:
        fail(4)

    config = OmniConfig(
        model=model,
        base_url=base_url,
        api_key=api_key,
        api_protocol="openai_responses",
        timeout=30.0,
    )
    started = time.monotonic()
    normalized = await call_omni(payload, config, type="on_demand")
    latency_ms = round((time.monotonic() - started) * 1000)
    output = parse_omni_response(normalized)
    output_present = bool(output.caption)
    if not output_present:
        fail(4)
    return latency_ms, image_count, output_present, extract_usage(normalized)


logging.disable(logging.CRITICAL)
try:
    latency_ms, image_count, output_present, usage = asyncio.run(run())
except KeyboardInterrupt:
    fail(130)
except SystemExit:
    raise
except BaseException:
    fail(4)

model = os.environ["MILOCO_RESPONSES_MODEL"]
print("protocol=openai_responses")
print(f"model={model}")
print(f"latency_ms={latency_ms}")
print(f"image_count={image_count}")
print(f"output_present={'true' if output_present else 'false'}")
print(f"input_tokens={usage['input_tokens']}")
print(f"output_tokens={usage['output_tokens']}")
print(f"cached_tokens={usage['cached_tokens']}")
PY
