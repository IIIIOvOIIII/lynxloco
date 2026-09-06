"""Offline acceptance of the synthetic Responses smoke runner; no providers."""

import asyncio
import base64
import importlib.util
import io
import json
from collections import Counter
from pathlib import Path

import httpx
import pytest
from PIL import Image

SCRIPT = Path(__file__).resolve().parents[1] / "accept-omni-concurrency.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("accept_omni_concurrency", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def payload(label):
    expected = {
        "red": {"object": "square", "color": "red", "event": True},
        "blue": {"object": "circle", "color": "blue", "event": True},
        "green": {"object": "triangle", "color": "green", "event": True},
        "blank": {"object": "none", "color": "none", "event": False},
    }[label]
    return {"status": "completed", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": json.dumps(expected)}
    ]}]}


def image_label(request):
    body = json.loads(request.content)
    parts = body["input"][0]["content"]
    images = [part for part in parts if part["type"] == "input_image"]
    assert len(images) == 1
    assert body["stream"] is False
    data = base64.b64decode(images[0]["image_url"].split(",", 1)[1])
    image = Image.open(io.BytesIO(data)).convert("RGB")
    pixels = Counter(image.get_flattened_data())
    if pixels[(255, 0, 0)]:
        return "red"
    if pixels[(0, 0, 255)]:
        return "blue"
    if pixels[(0, 180, 0)]:
        return "green"
    return "blank"


@pytest.mark.parametrize("concurrency", [1, 4, 8])
def test_fixed_40_images_complete_with_exact_cap_and_no_secret_output(concurrency):
    runner = load_runner()
    inflight = peak = requests = 0
    seen_labels = Counter()
    secret = "offline-secret-never-report"

    async def endpoint(request):
        nonlocal inflight, peak, requests
        assert request.url.path == "/v1/responses"
        assert request.headers["Authorization"] == f"Bearer {secret}"
        label = image_label(request)
        seen_labels[label] += 1
        requests += 1
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.002)
        inflight -= 1
        return httpx.Response(200, json=payload(label))

    report = asyncio.run(runner.run_acceptance(
        base_url="https://offline.test/v1", model="synthetic-model",
        concurrency=concurrency, api_key=secret, transport=httpx.MockTransport(endpoint),
    ))
    assert requests == 40
    assert seen_labels == {"red": 10, "blue": 10, "green": 10, "blank": 10}
    assert peak == concurrency == report["peak_inflight"]
    assert report["completed_count"] == report["matched_count"] == 40
    assert report["failure_count"] == 0
    assert report["image_count"] == 40
    assert report["passed"] is True
    serialized = json.dumps(report)
    assert secret not in serialized
    assert "data:image" not in serialized
    assert "input_text" not in serialized
    assert "output_text" not in serialized
    assert "not representative" in report["scope"]


def test_bad_responses_are_counted_without_retry_or_provider_text_leak():
    runner = load_runner()
    calls = 0
    secret = "private-key-reflected-in-error"

    async def endpoint(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, text=secret)
        if calls == 2:
            return httpx.Response(200, json={"status": "completed", "output": []})
        if calls == 3:
            response = payload("blank")
            response["output"][0]["content"][0]["text"] = '{"object":"none","color":"none","event":0}'
            return httpx.Response(200, json=response)
        if calls == 4:
            raise httpx.ReadTimeout(secret)
        if calls == 5:
            response = payload("blank")
            response["output"][0]["content"][0]["text"] = secret
            return httpx.Response(200, json=response)
        return httpx.Response(200, json=payload(image_label(request)))

    report = asyncio.run(runner.run_acceptance(
        base_url="https://offline.test/v1", model="test", concurrency=8,
        api_key=secret, transport=httpx.MockTransport(endpoint),
    ))
    assert calls == 40
    assert report["failure_count"] == 5
    assert report["matched_count"] == 35
    assert report["passed"] is False
    assert secret not in json.dumps(report)
    assert report["errors"] == {
        "http_429": 1, "missing_output": 1, "invalid_labels": 1,
        "request_timeout": 1, "invalid_json": 1,
    }


def test_deadline_cancels_inflight_and_never_sends_queued_requests():
    runner = load_runner()
    calls = 0

    async def endpoint(request):
        nonlocal calls
        calls += 1
        await asyncio.sleep(1)
        return httpx.Response(200, json=payload("blank"))

    report = asyncio.run(runner.run_acceptance(
        base_url="https://offline.test/v1", model="test", concurrency=4,
        api_key="offline", transport=httpx.MockTransport(endpoint), deadline_seconds=0.03,
    ))
    assert calls == report["request_count"] == 4
    assert report["deadline_exceeded"] is True
    assert report["failure_count"] == 40
    assert report["not_started_count"] == 36
    assert report["peak_inflight"] == 4


def test_deterministic_cards_have_same_bytes_and_labels_across_runs():
    runner = load_runner()
    assert runner.make_cards() == runner.make_cards()
    assert len(runner.make_cards()) == 40


@pytest.mark.parametrize("concurrency", [0, 9, 1.5, True])
def test_invalid_concurrency_does_not_make_requests(concurrency):
    runner = load_runner()
    with pytest.raises(ValueError, match="invalid_concurrency"):
        asyncio.run(runner.run_acceptance(
            base_url="https://offline.test/v1", model="test", concurrency=concurrency,
            api_key="offline", transport=httpx.MockTransport(lambda _: pytest.fail("no request")),
        ))


@pytest.mark.parametrize("raw,error", [
    ({"status": "incomplete", "output": []}, "response_not_completed"),
    ({"status": "completed", "output_text": "untrusted", "output": []}, "missing_output"),
    ({"status": "completed", "output": [{"content": [{"type": "output_text", "text": '{"object":"none","color":"none","event":false,"extra":1}'}]}]}, "invalid_labels"),
    ({"status": "completed", "output": [{"content": [{"type": "output_text", "text": '{"object":"none","color":"none","event":true,"event":false}'}]}]}, "invalid_json"),
])
def test_strict_provider_response_contract(raw, error):
    runner = load_runner()
    with pytest.raises(runner.ContractError, match=f"^{error}$"):
        runner.parse_labels(raw)


def test_cli_key_is_stdin_only_and_report_and_stdout_are_redacted(monkeypatch, tmp_path, capsys):
    runner = load_runner()
    secret = "stdin-only-secret"
    monkeypatch.setenv("MILOCO_MODEL__OMNI__API_KEY", "wrong-environment-key")
    monkeypatch.setattr(runner.sys, "stdin", io.StringIO(secret + "\n"))

    async def offline_run(**kwargs):
        assert kwargs["api_key"] == secret
        assert kwargs["concurrency"] == 8
        return {"scope": runner.SCOPE, "passed": True, "matched_count": 40}

    monkeypatch.setattr(runner, "run_acceptance", offline_run)
    output = tmp_path / "result.json"
    exit_code = runner.main([
        "--base-url", "https://offline.test/v1", "--model", "test",
        "--concurrency", "8", "--output", str(output),
    ])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(output.read_text())["matched_count"] == 40
    assert secret not in output.read_text() + captured.out + captured.err
    assert "wrong-environment-key" not in output.read_text() + captured.out + captured.err


def test_unexpected_cli_error_does_not_expose_secret_or_error_text(monkeypatch, tmp_path, capsys):
    runner = load_runner()
    secret = "reflected-private-token"
    monkeypatch.setattr(runner.sys, "stdin", io.StringIO(secret))

    async def failing_run(**kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(runner, "run_acceptance", failing_run)
    assert runner.main([
        "--base-url", "https://offline.test/v1", "--model", "test",
        "--concurrency", "8", "--output", str(tmp_path / "unused.json"),
    ]) == 2
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert "runner_error" in captured.out


def test_report_cannot_pass_without_exercising_requested_concurrency():
    runner = load_runner()

    def immediate_endpoint(request):
        return httpx.Response(200, json=payload(image_label(request)))

    report = asyncio.run(runner.run_acceptance(
        base_url="https://offline.test/v1", model="test", concurrency=8,
        api_key="offline", transport=httpx.MockTransport(immediate_endpoint),
    ))
    assert report["matched_count"] == 40
    assert report["peak_inflight"] == 1
    assert report["concurrency_exercised"] is False
    assert report["passed"] is False
