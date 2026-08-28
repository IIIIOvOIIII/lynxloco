"""Strict local HTTP fixture for OpenAI Responses integration tests.

The fixture stores only bounded request metadata. Raw request bodies,
Authorization values, and image data URLs never enter its records or logs.
"""

from __future__ import annotations

import base64
import binascii
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    model: str | None
    image_count: int
    stream: bool
    auth_present: bool


class ResponsesFixtureServer:
    """Real local HTTP server enforcing the supported Responses v1 contract."""

    def __init__(
        self,
        *,
        api_key: str = "",
        models_status: int = 200,
        responses_status: int = 200,
        malformed_response: bool = False,
        hang_perception: bool = False,
    ) -> None:
        if models_status not in {200, 404, 405}:
            raise ValueError("models_status must be 200, 404, or 405")
        self.api_key = api_key
        self.models_status = models_status
        self.responses_status = responses_status
        self.malformed_response = malformed_response
        self.hang_perception = hang_perception
        self.perception_hang_started = threading.Event()
        self._release_perception_hang = threading.Event()
        self.requests: list[RecordedRequest] = []
        self._requests_lock = threading.Lock()
        owner = self

        class Handler(_ResponsesHandler):
            fixture = owner

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host = str(self._server.server_address[0])
        port = int(self._server.server_address[1])
        return f"http://{host}:{port}/v1"

    def record(self, request: RecordedRequest) -> None:
        with self._requests_lock:
            self.requests.append(request)

    def __enter__(self) -> "ResponsesFixtureServer":
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._release_perception_hang.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


class _ResponsesHandler(BaseHTTPRequestHandler):
    fixture: ResponsesFixtureServer
    server_version = "MilocoResponsesFixture/1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/v1/models":
            self._json_error(404, "wrong_path")
            return
        if not self._auth_ok():
            self._json_error(401, "wrong_auth")
            return
        self.fixture.record(
            RecordedRequest(
                method="GET",
                path=self.path,
                model=None,
                image_count=0,
                stream=False,
                auth_present="Authorization" in self.headers,
            )
        )
        if self.fixture.models_status != 200:
            self._json_error(self.fixture.models_status, "models_unsupported")
            return
        self._json_response(
            200,
            {"object": "list", "data": [{"id": "fixture-vlm", "object": "model"}]},
        )

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/responses":
            self._json_error(404, "wrong_path")
            return
        if not self._auth_ok():
            self._json_error(401, "wrong_auth")
            return
        body = self._read_json()
        if body is None:
            return
        validation = _validate_request(body)
        if isinstance(validation, str):
            self._json_error(400, validation)
            return
        image_count, stream = validation
        model = body.get("model")
        self.fixture.record(
            RecordedRequest(
                method="POST",
                path=self.path,
                model=model if isinstance(model, str) else None,
                image_count=image_count,
                stream=stream,
                auth_present="Authorization" in self.headers,
            )
        )
        if self.fixture.responses_status != 200:
            self._json_error(self.fixture.responses_status, "fixture_failure")
            return
        if self.fixture.malformed_response:
            self._json_response(200, {"id": "resp_malformed", "output": []})
            return
        if self.fixture.hang_perception and body.get("max_output_tokens") != 16:
            self.fixture.perception_hang_started.set()
            self.fixture._release_perception_hang.wait(timeout=60)
            return

        output_text = (
            "red"
            if body.get("max_output_tokens") == 16
            else json.dumps(
                {
                    "caption": "fixture saw the room",
                    "matched_rules": [],
                    "speeches": [],
                    "suggestions": [],
                },
                separators=(",", ":"),
            )
        )
        if stream:
            self._sse_response(output_text)
        else:
            self._json_response(200, _responses_body(output_text))

    def _auth_ok(self) -> bool:
        actual = self.headers.get("Authorization")
        expected = f"Bearer {self.fixture.api_key}" if self.fixture.api_key else None
        return actual == expected

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json_error(400, "invalid_content_length")
            return None
        if length <= 0 or length > 8 * 1024 * 1024:
            self._json_error(400, "invalid_content_length")
            return None
        try:
            decoded = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json_error(400, "invalid_json")
            return None
        if not isinstance(decoded, dict):
            self._json_error(400, "invalid_json")
            return None
        return decoded

    def _sse_response(self, output_text: str) -> None:
        midpoint = max(1, len(output_text) // 2)
        chunks = (output_text[:midpoint], output_text[midpoint:])
        events: list[tuple[str, dict[str, Any]]] = []
        for chunk in chunks:
            if chunk:
                events.append(
                    (
                        "response.output_text.delta",
                        {"type": "response.output_text.delta", "delta": chunk},
                    )
                )
        events.append(
            (
                "response.completed",
                {"type": "response.completed", "response": {"usage": _usage()}},
            )
        )
        wire = b"".join(
            f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()
            for event, data in events
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(wire)))
        self.end_headers()
        self.wfile.write(wire)

    def _json_response(self, status: int, body: dict[str, Any]) -> None:
        wire = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(wire)))
        self.end_headers()
        self.wfile.write(wire)

    def _json_error(self, status: int, code: str) -> None:
        self._json_response(status, {"error": {"code": code}})

    def log_message(self, format: str, *args: Any) -> None:
        return


def _validate_request(body: dict[str, Any]) -> tuple[int, bool] | str:
    allowed = {"model", "instructions", "input", "max_output_tokens", "stream"}
    if set(body) - allowed:
        return "unsupported_field"
    if not isinstance(body.get("model"), str) or not body["model"]:
        return "missing_model"
    if not isinstance(body.get("instructions"), str):
        return "missing_instructions"
    if not isinstance(body.get("max_output_tokens"), int):
        return "missing_max_output_tokens"
    if not isinstance(body.get("stream"), bool):
        return "missing_stream"
    inputs = body.get("input")
    if not isinstance(inputs, list) or len(inputs) != 1:
        return "invalid_input"
    user = inputs[0]
    if not isinstance(user, dict) or set(user) != {"role", "content"}:
        return "invalid_input"
    if user.get("role") != "user" or not isinstance(user.get("content"), list):
        return "invalid_input"

    image_count = 0
    text_count = 0
    for block in user["content"]:
        if not isinstance(block, dict):
            return "invalid_content"
        block_type = block.get("type")
        if block_type == "input_text":
            if set(block) != {"type", "text"} or not isinstance(block.get("text"), str):
                return "invalid_text"
            text_count += 1
            continue
        if block_type == "input_image":
            if set(block) != {"type", "image_url"}:
                return "invalid_image"
            image_url = block.get("image_url")
            if not isinstance(image_url, str) or not image_url.startswith(
                "data:image/jpeg;base64,"
            ):
                return "invalid_image"
            try:
                raw = base64.b64decode(image_url.split(",", 1)[1], validate=True)
            except (ValueError, binascii.Error):
                return "invalid_image"
            if not raw.startswith(b"\xff\xd8") or not raw.endswith(b"\xff\xd9"):
                return "invalid_image"
            image_count += 1
            continue
        return "unsupported_media"
    if text_count < 1:
        return "missing_input_text"
    if image_count < 1:
        return "missing_input_image"
    if image_count > 12:
        return "too_many_images"
    return image_count, body["stream"]


def _usage() -> dict[str, Any]:
    return {
        "input_tokens": 23,
        "output_tokens": 7,
        "total_tokens": 30,
        "input_tokens_details": {"cached_tokens": 3},
    }


def _responses_body(output_text: str) -> dict[str, Any]:
    return {
        "id": "resp_fixture",
        "object": "response",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": output_text}],
            }
        ],
        "usage": _usage(),
    }
