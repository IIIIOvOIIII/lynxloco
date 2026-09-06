"""Bounded window lifetime and process-wide, camera-fair Omni admission.

Preparation and commit locks belong to the inference loop. Completion futures
cross the worker/main-loop boundary so final rule effects complete before the
next window commits. HTTP permits are thread-safe, including worker restarts.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict, deque
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)

MAX_WAITING = 8
MAX_WAITING_PER_CAMERA = 4
MAX_PAYLOAD_BYTES = 128 * 1024 * 1024
MAX_WINDOW_AGE_SECONDS = 180.0


def concurrency() -> int:
    from miloco.config import get_settings

    value = getattr(get_settings().model.omni, "concurrency", 1)
    return max(1, min(8, int(value)))


def model_identity(config=None) -> str:
    """Credential-free digest of the selected provider identity."""
    if config is None:
        from miloco.config import get_settings

        config = get_settings().model.omni
    from miloco.perception.engine.omni.provider import resolve_api_protocol

    values = [
        config.model,
        str(resolve_api_protocol(config.api_protocol, config.model)),
        config.base_url.strip().rstrip("/"),
    ]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()[:16]


def request_version(config) -> str:
    values = [
        model_identity(config),
        config.timeout,
        config.max_completion_tokens,
        config.temperature,
        config.top_p,
        config.stream,
    ]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()[:16]


class WindowRejected(RuntimeError):
    """Explicit capacity/age rejection, never a negative perception result."""


@dataclass(eq=False)
class WindowJob:
    camera: str
    generation: int
    sequence: int
    end_unix_ms: float
    previous: WindowJob | None = None
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    config_version: str = ""
    model_identity: str = ""
    http_started_count: int = 0
    http_finished_count: int = 0
    http_error_code: str | None = None
    http_ms: float = 0.0
    slot_wait_ms: float = 0.0
    reorder_wait_ms: float = 0.0
    trace_timing: dict = field(default_factory=dict)
    _http_live_started: float | None = None
    valid: bool = True
    completed: concurrent.futures.Future = field(
        default_factory=concurrent.futures.Future
    )
    prepared: concurrent.futures.Future = field(
        default_factory=concurrent.futures.Future
    )
    prepare_lock: asyncio.Lock | None = None
    owns_lock: bool = False
    committing: bool = False
    release_media: list[Callable[[], None]] = field(default_factory=list)
    cleanup: list[Callable[[], None]] = field(default_factory=list)
    _lifetime_lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def actionable(self) -> bool:
        return (
            self.valid
            and time.time() * 1000 - self.end_unix_ms <= MAX_WINDOW_AGE_SECONDS * 1000
            and (not self.model_identity or self.model_identity == model_identity())
        )

    def start_http(self, waiting_started: float) -> None:
        with self._lifetime_lock:
            now = time.monotonic()
            self.slot_wait_ms += (now - waiting_started) * 1000
            self.http_started_count += 1
            self._http_live_started = now

    def end_http(self, error: BaseException | None = None) -> None:
        with self._lifetime_lock:
            if self._http_live_started is not None:
                self.http_ms += (time.monotonic() - self._http_live_started) * 1000
                self._http_live_started = None
                self.http_finished_count += 1
                self.http_error_code = (
                    type(error).__name__ if error is not None else None
                )

    def http_elapsed_ms(self) -> float:
        with self._lifetime_lock:
            live = (
                0.0
                if self._http_live_started is None
                else (time.monotonic() - self._http_live_started) * 1000
            )
            return self.http_ms + live

    def release_preparation(self) -> None:
        for release in self.release_media:
            release()
        self.release_media.clear()
        if self.owns_lock and not self.committing and self.prepare_lock is not None:
            self.prepare_lock.release()
            self.owns_lock = False
        with self._lifetime_lock:
            if not self.prepared.done():
                self.prepared.set_result(None)

    async def begin_commit(self) -> bool:
        if self.committing:
            return self.actionable
        self.release_preparation()
        wait_started = time.monotonic()
        if self.previous is not None:
            # The predecessor has its own bounded lifetime; shield prevents this
            # waiter's cancellation from cancelling its completion marker.
            predecessor = self.previous
            remaining = max(
                0, predecessor.end_unix_ms / 1000 + MAX_WINDOW_AGE_SECONDS - time.time()
            )
            try:
                await asyncio.wait_for(
                    asyncio.shield(asyncio.wrap_future(predecessor.completed)),
                    remaining,
                )
            except TimeoutError:
                predecessor.valid = False
                predecessor.finish()
                logger.warning(
                    "[windows] reorder deadline gap camera=%s sequence=%d",
                    predecessor.camera,
                    predecessor.sequence,
                )
        if self.prepare_lock is not None:
            await self.prepare_lock.acquire()
            self.owns_lock = True
        self.committing = True
        self.reorder_wait_ms += (time.monotonic() - wait_started) * 1000
        return self.actionable

    def release_state(self) -> None:
        if self.owns_lock and self.prepare_lock is not None:
            self.prepare_lock.release()
            self.owns_lock = False

    def retain_cleanup(self, cleanup: Callable[[], None]) -> bool:
        with self._lifetime_lock:
            if self.completed.done():
                return False
            self.cleanup.append(cleanup)
            return True

    def finish(self) -> None:
        # Completion can race worker cleanup when the main caller is cancelled.
        # Never touch an inference-loop asyncio.Lock from this method.
        with self._lifetime_lock:
            if not self.prepared.done():
                self.prepared.set_result(None)
            cleanups, self.cleanup = self.cleanup, []
            if not self.completed.done():
                self.completed.set_result(None)
            self.previous = None
        for cleanup in cleanups:
            cleanup()


_current_job: ContextVar[WindowJob | None] = ContextVar(
    "perception_window", default=None
)


def current_job() -> WindowJob | None:
    return _current_job.get()


@contextmanager
def window_scope(job: WindowJob | None):
    token = _current_job.set(job)
    try:
        yield
    finally:
        _current_job.reset(token)


@dataclass(eq=False)
class _Waiter:
    camera: str
    size: int
    ready: concurrent.futures.Future = field(default_factory=concurrent.futures.Future)
    granted: bool = False


class RequestLimiter:
    def __init__(
        self,
        limit: Callable[[], int] = concurrency,
        *,
        max_payload_bytes: int = MAX_PAYLOAD_BYTES,
    ):
        self._limit = limit
        self._max_bytes = max_payload_bytes
        self._lock = threading.Lock()
        self._waiting: OrderedDict[str, deque[_Waiter]] = OrderedDict()
        self.active = 0
        self.peak = 0
        self.peak_at_limit = 0
        self._last_limit = None
        self.submitted = 0
        self.http_started = 0
        self.completed = 0
        self.payload_bytes = 0
        self.rejected: dict[str, int] = {}

    def _reject(self, reason: str):
        self.rejected[reason] = self.rejected.get(reason, 0) + 1
        raise WindowRejected(reason)

    def _pump(self) -> None:
        limit = self._limit()
        if limit != self._last_limit:
            logger.info(
                "[omni-concurrency] limit=%d previous=%s active=%d waiting=%d bytes=%d",
                limit,
                self._last_limit,
                self.active,
                sum(map(len, self._waiting.values())),
                self.payload_bytes,
            )
            self._last_limit = limit
            self.peak_at_limit = 0
        while self._waiting and self.active < limit:
            camera, queue = self._waiting.popitem(last=False)
            waiter = queue.popleft()
            if queue:
                self._waiting[camera] = queue
            waiter.granted = True
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.submitted += 1
            if self.active > self.peak_at_limit:
                self.peak_at_limit = self.active
                logger.info(
                    "[omni-concurrency] limit=%d peak=%d active=%d waiting=%d bytes=%d submitted=%d completed=%d",
                    limit,
                    self.peak_at_limit,
                    self.active,
                    sum(map(len, self._waiting.values())),
                    self.payload_bytes,
                    self.submitted,
                    self.completed,
                )
            waiter.ready.set_result(None)

    @asynccontextmanager
    async def slot(
        self, camera: str, payload_bytes: int, *, retain_for: WindowJob | None = None
    ):
        waiter = _Waiter(camera, payload_bytes)
        with self._lock:
            if self.payload_bytes + payload_bytes > self._max_bytes:
                self._reject("payload_bytes")
            queue = self._waiting.get(camera, ())
            if self.active >= self._limit():
                if sum(map(len, self._waiting.values())) >= MAX_WAITING:
                    self._reject("waiting_full")
                if len(queue) >= MAX_WAITING_PER_CAMERA:
                    self._reject("camera_waiting_full")
            self.payload_bytes += payload_bytes
            self._waiting.setdefault(camera, deque()).append(waiter)
            self._pump()
        try:
            await asyncio.shield(asyncio.wrap_future(waiter.ready))
            yield
        finally:
            with self._lock:
                if waiter.granted:
                    self.active -= 1
                    self.completed += 1
                else:
                    queue = self._waiting.get(camera)
                    if queue is not None and waiter in queue:
                        queue.remove(waiter)
                        if not queue:
                            del self._waiting[camera]

                def release_bytes():
                    with self._lock:
                        self.payload_bytes -= payload_bytes

                retained = (
                    retain_for is not None
                    and waiter.granted
                    and retain_for.retain_cleanup(release_bytes)
                )
                if not retained:
                    self.payload_bytes -= payload_bytes
                self._pump()

    def snapshot(self) -> dict:
        limit = self._limit()
        with self._lock:
            return {
                "limit": limit,
                "active": self.active,
                "peak": self.peak,
                "peak_at_limit": self.peak_at_limit,
                "submitted": self.submitted,
                "http_started": self.http_started,
                "completed": self.completed,
                "waiting": sum(map(len, self._waiting.values())),
                "payload_bytes": self.payload_bytes,
                "rejected": dict(self.rejected),
            }


HTTP_LIMITER = RequestLimiter()


@asynccontextmanager
async def model_request(body: dict):
    """Bound the actual encoded HTTP request, including stream consumption."""
    job = current_job()
    if job is not None:
        job.release_preparation()
        if not job.actionable:
            raise WindowRejected("window_age")
    # Count encoded body plus serialization copy. This is deliberately
    # conservative; the retained JPEG/clip artifact copy is smaller than base64.
    size = 2 * len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
    waiting_started = time.monotonic()
    granted = False
    try:
        async with asyncio.timeout(MAX_WINDOW_AGE_SECONDS):
            async with HTTP_LIMITER.slot(
                job.camera if job else "on_demand", size, retain_for=job
            ):
                if job is not None and not job.actionable:
                    raise WindowRejected("window_age_or_config")
                granted = True
                with HTTP_LIMITER._lock:
                    HTTP_LIMITER.http_started += 1
                if job is not None:
                    job.start_http(waiting_started)
                error = None
                try:
                    yield
                except BaseException as exc:
                    error = exc
                    raise
                finally:
                    if job is not None:
                        job.end_http(error)
    finally:
        if job is not None and not granted:
            job.slot_wait_ms += (time.monotonic() - waiting_started) * 1000
