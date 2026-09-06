#!/usr/bin/env python3
"""Synthetic Responses visual/JSON smoke. Not representative of camera workloads."""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import logging
import math
import os
import random
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageDraw

SEED = 20260906
WINDOW_COUNT = 40
DEADLINE_SECONDS = 600
SCOPE = (
    "SYNTHETIC endpoint visual/JSON contract and same-input concurrency smoke only; "
    "not representative of camera latency or people/pet accuracy. This does not "
    "verify the Miloco runtime HTTP cap or real-camera throughput."
)
EXPECTED = {
    "red": {"object": "square", "color": "red", "event": True},
    "blue": {"object": "circle", "color": "blue", "event": True},
    "green": {"object": "triangle", "color": "green", "event": True},
    "blank": {"object": "none", "color": "none", "event": False},
}
INSTRUCTIONS = (
    "Inspect the single image. Return only one JSON object with exactly these "
    "three keys: object, color, event. object must be square, circle, triangle, "
    "or none. color must be red, blue, green, or none. event must be the JSON "
    "boolean true when a colored geometric object is present, and false when "
    "the image is blank. For a blank image use object=none and color=none. "
    "Do not include Markdown or explanations."
)


def make_cards() -> list[tuple[str, str]]:
    """Generate the same forty labelled PNG data URLs entirely in memory."""
    rng = random.Random(SEED)
    labels = [label for label in EXPECTED for _ in range(10)]
    rng.shuffle(labels)
    cards = []
    for label in labels:
        image = Image.new("RGB", (320, 240), "white")
        draw = ImageDraw.Draw(image)
        x, y = 160 + rng.randint(-20, 20), 120 + rng.randint(-15, 15)
        if label == "red":
            draw.rectangle((x - 55, y - 55, x + 55, y + 55), fill=(255, 0, 0))
        elif label == "blue":
            draw.ellipse((x - 55, y - 55, x + 55, y + 55), fill=(0, 0, 255))
        elif label == "green":
            draw.polygon(((x, y - 60), (x - 60, y + 50), (x + 60, y + 50)), fill=(0, 180, 0))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        cards.append((label, f"data:image/png;base64,{encoded}"))
    return cards


class ContractError(ValueError):
    """Carries only a fixed error class, never provider content."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("invalid_json")
        result[key] = value
    return result


def parse_labels(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ContractError("invalid_response")
    if raw.get("status") != "completed":
        raise ContractError("response_not_completed")
    output = raw.get("output")
    if not isinstance(output, list):
        raise ContractError("missing_output")
    parts = []
    for item in output:
        if not isinstance(item, dict):
            raise ContractError("invalid_response")
        content = item.get("content", [])
        if not isinstance(content, list):
            raise ContractError("invalid_response")
        for part in content:
            if not isinstance(part, dict):
                raise ContractError("invalid_response")
            if part.get("type") == "output_text":
                text = part.get("text")
                if not isinstance(text, str):
                    raise ContractError("missing_output")
                parts.append(text)
    text = "".join(parts)
    if not text.strip():
        raise ContractError("missing_output")
    try:
        labels = json.loads(text, object_pairs_hook=_unique_object)
    except (ValueError, TypeError):
        raise ContractError("invalid_json") from None
    if (
        not isinstance(labels, dict)
        or set(labels) != {"object", "color", "event"}
        or labels["object"] not in ("square", "circle", "triangle", "none")
        or labels["color"] not in ("red", "blue", "green", "none")
        or type(labels["event"]) is not bool
    ):
        raise ContractError("invalid_labels")
    return labels


async def run_acceptance(
    *,
    base_url: str,
    model: str,
    concurrency: int,
    api_key: str,
    transport: httpx.AsyncBaseTransport | None = None,
    deadline_seconds: float = DEADLINE_SECONDS,
) -> dict:
    if type(concurrency) is not int or not 1 <= concurrency <= 8:
        raise ValueError("invalid_concurrency")
    if not math.isfinite(deadline_seconds) or not 0 < deadline_seconds <= DEADLINE_SECONDS:
        raise ValueError("invalid_deadline")
    url = urlsplit(base_url)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
    ):
        raise ValueError("invalid_base_url")
    if not model.strip() or "\n" in api_key or "\r" in api_key:
        raise ValueError("invalid_input")

    started = time.monotonic()
    cards = make_cards()
    results = [
        {"window": i + 1, "status": "not_started", "error_class": None,
         "http_status": None, "latency_ms": None, "matched": False, "image_count": 0}
        for i in range(WINDOW_COUNT)
    ]
    indices = iter(range(WINDOW_COUNT))
    inflight = peak = request_count = 0
    deadline_exceeded = False
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(min(120.0, deadline_seconds)),
        limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
        transport=transport,
        follow_redirects=False,
        trust_env=False,
    ) as client:

        async def worker():
            nonlocal inflight, peak, request_count
            for index in indices:
                label, image_url = cards[index]
                result = results[index]
                call_started = time.monotonic()
                inflight += 1
                peak = max(peak, inflight)
                request_count += 1
                result.update(status="failed", image_count=1)
                try:
                    response = await client.post(
                        f"{base_url.rstrip('/')}/responses",
                        json={
                            "model": model,
                            "instructions": INSTRUCTIONS,
                            "input": [{"role": "user", "content": [
                                {"type": "input_text", "text": "Classify this image."},
                                {"type": "input_image", "image_url": image_url},
                            ]}],
                            "max_output_tokens": 2048,
                            "stream": False,
                        },
                    )
                    result["http_status"] = response.status_code
                    if not 200 <= response.status_code < 300:
                        raise ContractError(f"http_{response.status_code}")
                    try:
                        raw = response.json()
                    except ValueError:
                        raise ContractError("invalid_response_json") from None
                    labels = parse_labels(raw)
                    if labels != EXPECTED[label]:
                        raise ContractError("label_mismatch")
                    result.update(status="passed", matched=True)
                except ContractError as error:
                    result["error_class"] = str(error)
                except httpx.TimeoutException:
                    result["error_class"] = "request_timeout"
                except httpx.TransportError:
                    result["error_class"] = "transport_error"
                except asyncio.CancelledError:
                    result["error_class"] = "deadline"
                    raise
                except Exception:
                    result["error_class"] = "internal_error"
                finally:
                    result["latency_ms"] = round((time.monotonic() - call_started) * 1000, 2)
                    inflight -= 1

        remaining = max(0, deadline_seconds - (time.monotonic() - started))
        try:
            async with asyncio.timeout(remaining):
                await asyncio.gather(*(worker() for _ in range(concurrency)))
        except TimeoutError:
            deadline_exceeded = True

    for result in results:
        if result["status"] == "not_started":
            result["error_class"] = "deadline_not_started"
    matched = sum(result["matched"] for result in results)
    completed = sum(
        result["status"] != "not_started" and result["error_class"] != "deadline"
        for result in results
    )
    return {
        "scope": SCOPE,
        "dataset": "synthetic-geometric-cards-v1",
        "seed": SEED,
        "concurrency": concurrency,
        "deadline_seconds": deadline_seconds,
        "deadline_exceeded": deadline_exceeded,
        "planned_count": WINDOW_COUNT,
        "request_count": request_count,
        "completed_count": completed,
        "not_started_count": sum(result["status"] == "not_started" for result in results),
        "image_count": sum(result["image_count"] for result in results),
        "peak_inflight": peak,
        "concurrency_exercised": peak == concurrency,
        "matched_count": matched,
        "failure_count": WINDOW_COUNT - matched,
        "passed": matched == WINDOW_COUNT and not deadline_exceeded and peak == concurrency,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
        "errors": dict(Counter(result["error_class"] for result in results if result["error_class"])),
        "labels": {
            label: {"expected_count": 10, "matched_count": sum(
                result["matched"] for (card_label, _), result in zip(cards, results)
                if card_label == label
            )}
            for label in EXPECTED
        },
        "windows": results,
    }


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=SCOPE)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--concurrency", required=True, type=int, choices=range(1, 9))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    logging.disable(logging.CRITICAL)
    try:
        # The pipe must close after the key; no environment/argument credential fallback.
        api_key = sys.stdin.read(65537).strip()
        if len(api_key) > 65536:
            raise ValueError("invalid_input")
        report = asyncio.run(run_acceptance(
            base_url=args.base_url, model=args.model, concurrency=args.concurrency,
            api_key=api_key,
        ))
        write_report(args.output, report)
    except KeyboardInterrupt:
        print(json.dumps({"scope": SCOPE, "passed": False, "error_class": "interrupted"}))
        return 130
    except Exception:
        print(json.dumps({"scope": SCOPE, "passed": False, "error_class": "runner_error"}))
        return 2
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
