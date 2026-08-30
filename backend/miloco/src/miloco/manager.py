# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Service manager module
"""

import asyncio
import logging
import struct
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from starlette.websockets import WebSocketState

from miloco.camera.service import CameraService
from miloco.camera.stream import (
    EncodedVideoPacket,
    LiveJpegStreamHub,
    LiveStreamHub,
    LiveStreamSource,
)
from miloco.config import get_settings
from miloco.database.kv_repo import KVRepo, SystemConfigKeys
from miloco.database.person_repo import PersonRepo
from miloco.home_profile.service import HomeProfileService
from miloco.miot.client import MiotProxy
from miloco.miot.service import MiotService
from miloco.node_monitor import NodeKind, NodeName, get_monitor
from miloco.perception import init_perception_module
from miloco.perception.service import PerceptionService
from miloco.person.service import PersonService
from miloco.rule.service import RuleService, init_rule_service
from miloco.rule.terminate_evaluator import TerminateEvaluator
from miloco.task.service import TaskService

logger = logging.getLogger(__name__)


class _MiotListenerWebSocket:
    """Adapt the legacy MIoT fan-out wire format to packet callbacks."""

    def __init__(self, owner: "_MiotLiveStreamBackend") -> None:
        self._owner = owner
        self.client_state = WebSocketState.CONNECTED

    async def send_text(self, _message: str) -> None:
        # The unified endpoint has a fixed binary-only H.264 contract.
        return

    async def send_bytes(self, payload: bytes) -> None:
        self._owner.publish_legacy_payload(payload)

    async def close(self) -> None:
        self.client_state = WebSocketState.DISCONNECTED

    def reopen(self) -> None:
        self.client_state = WebSocketState.CONNECTED


class _MiotLiveStreamBackend:
    """Expose one legacy MIoT live subscription as a hub packet source."""

    _SHUTDOWN_TIMEOUT_SECONDS = 0.1

    def __init__(self, legacy_manager: Any, camera_id: str, channel: int) -> None:
        self._legacy_manager = legacy_manager
        self._camera_id = camera_id
        self._channel = channel
        self._listeners: dict[int, Callable[[EncodedVideoPacket], None]] = {}
        self._close_listeners: dict[int, Callable[[str | None], None]] = {}
        self._next_listener_id = 0
        self._connection_id: str | None = None
        self._start_task: asyncio.Task[None] | None = None
        self._start_generation: int | None = None
        self._start_tasks: set[asyncio.Task[None]] = set()
        self._close_task: asyncio.Task[None] | None = None
        self._generation = 0
        self._closed = False
        self._websocket = _MiotListenerWebSocket(self)

    def add_packet_listener(
        self, listener: Callable[[EncodedVideoPacket], None]
    ) -> Callable[[], None]:
        if self._closed:
            raise RuntimeError("MIoT live stream backend is closed")
        was_empty = not self._listeners
        listener_id = self._next_listener_id
        self._next_listener_id += 1
        self._listeners[listener_id] = listener
        if was_empty:
            self._generation += 1
        close_task = self._close_task
        close_pending = close_task is not None and not close_task.done()
        start_for_generation = (
            self._start_task is not None
            and not self._start_task.done()
            and self._start_generation == self._generation
        )
        should_start = not start_for_generation and (
            close_pending or self._connection_id is None
        )
        if should_start:
            self._create_start(
                generation=self._generation,
                preceding_close=close_task if close_pending else None,
            )

        def detach() -> None:
            self._listeners.pop(listener_id, None)
            if not self._listeners:
                self._generation += 1
                self._schedule_close()

        return detach

    def add_close_listener(
        self, listener: Callable[[str | None], None]
    ) -> Callable[[], None]:
        listener_id = self._next_listener_id
        self._next_listener_id += 1
        self._close_listeners[listener_id] = listener

        def detach() -> None:
            self._close_listeners.pop(listener_id, None)

        return detach

    def publish_legacy_payload(self, payload: bytes) -> None:
        if len(payload) < 16:
            return
        frame_type, timestamp = struct.unpack(">B7xQ", payload[:16])
        packet = EncodedVideoPacket(
            codec="h264",
            data=bytes(payload[16:]),
            pts=timestamp,
            dts=timestamp,
            is_keyframe=frame_type == 1,
            time_base_num=1,
            time_base_den=1000,
        )
        for listener in tuple(self._listeners.values()):
            try:
                listener(packet)
            except Exception:  # noqa: BLE001
                logger.warning("MIoT live stream listener failed")

    def _create_start(
        self,
        *,
        generation: int,
        preceding_close: asyncio.Task[None] | None = None,
    ) -> None:
        task = asyncio.create_task(
            self._start(generation=generation, preceding_close=preceding_close)
        )
        self._start_task = task
        self._start_generation = generation
        self._start_tasks.add(task)
        task.add_done_callback(self._start_tasks.discard)

    async def _start(
        self,
        *,
        generation: int,
        preceding_close: asyncio.Task[None] | None,
    ) -> None:
        try:
            if preceding_close is not None and not preceding_close.done():
                await asyncio.gather(preceding_close, return_exceptions=True)
            if self._closed or not self._listeners or generation != self._generation:
                return
            self._websocket.reopen()
            connection_id = await self._legacy_manager.new_connection(
                websocket=self._websocket,
                user_name="unified-camera-view",
                token_hash="internal",
                camera_id=self._camera_id,
                channel=self._channel,
            )
            if self._closed or not self._listeners or generation != self._generation:
                await self._close_connection_id(connection_id)
                return
            self._connection_id = connection_id
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("MIoT live stream subscription failed")
            if generation == self._generation and self._listeners:
                self._notify_closed("camera_unavailable")

    def _schedule_close(self) -> None:
        if self._close_task is None or self._close_task.done():
            start_task = self._start_task
            self._close_task = asyncio.create_task(
                self._close_connection(start_task=start_task)
            )

    async def _close_connection(
        self, *, start_task: asyncio.Task[None] | None = None
    ) -> None:
        if start_task is not None and start_task is not asyncio.current_task():
            await asyncio.gather(start_task, return_exceptions=True)
        connection_id, self._connection_id = self._connection_id, None
        if connection_id is None:
            return
        await self._close_connection_id(connection_id)

    async def _close_connection_id(self, connection_id: str) -> None:
        try:
            await self._legacy_manager.close_connection(
                user_name="unified-camera-view",
                token_hash="internal",
                camera_id=self._camera_id,
                channel=self._channel,
                cid=connection_id,
            )
        except Exception:  # noqa: BLE001
            logger.warning("MIoT live stream detach failed")

    def _notify_closed(self, error_code: str | None) -> None:
        for listener in tuple(self._close_listeners.values()):
            try:
                listener(error_code)
            except Exception:  # noqa: BLE001
                pass

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._generation += 1
        self._listeners.clear()
        self._close_listeners.clear()
        start_tasks = tuple(self._start_tasks)
        for task in start_tasks:
            task.cancel()
        if start_tasks:
            _done, pending = await asyncio.wait(
                start_tasks, timeout=self._SHUTDOWN_TIMEOUT_SECONDS
            )
            if pending:
                logger.warning("MIoT live stream start did not cancel before shutdown")
        close_task = self._close_task
        if close_task is not None and not close_task.done():
            close_task.cancel()
            _done, pending = await asyncio.wait(
                {close_task}, timeout=self._SHUTDOWN_TIMEOUT_SECONDS
            )
            if pending:
                logger.warning("MIoT live stream detach did not cancel before shutdown")
        connection_id, self._connection_id = self._connection_id, None
        if connection_id is not None:
            detach_task = asyncio.create_task(self._close_connection_id(connection_id))
            _done, pending = await asyncio.wait(
                {detach_task}, timeout=self._SHUTDOWN_TIMEOUT_SECONDS
            )
            if pending:
                detach_task.cancel()
                logger.warning("MIoT live stream detach timed out during shutdown")
        self._start_task = None
        self._start_generation = None
        self._close_task = None
        await self._websocket.close()


class Manager:
    """
    Service manager singleton class - simplified version
    Only responsible for service initialization and providing access interfaces, no business logic
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        pass

    async def initialize(self):
        """
        Initialize all services
        """
        if getattr(self, "_initialized", False):
            logger.debug(
                "Manager already initialized, skipping duplicate initialization"
            )
            return

        logger.info("Manager initialization started")

        mon = get_monitor()
        mon.register(NodeName.CAMERA, NodeKind.SOURCE, watchdog_s=60)
        mon.register(NodeName.COLLECTOR, NodeKind.WINDOW, watchdog_s=60)
        mon.register(NodeName.PROCESSOR, NodeKind.WINDOW, watchdog_s=60)
        mon.register(NodeName.ENGINE, NodeKind.WINDOW, watchdog_s=60)
        mon.register(NodeName.RULE, NodeKind.EVENT, watchdog_s=60)
        mon.register(NodeName.MIOT_PROXY, NodeKind.SERVICE)
        mon.register(NodeName.RULE_SERVICE, NodeKind.SERVICE)
        mon.register(NodeName.PERCEPTION_SERVICE, NodeKind.SERVICE)
        mon.register(NodeName.TERMINATE_EVALUATOR, NodeKind.SERVICE)

        # Initialize repo layer
        self._kv_repo = KVRepo()
        self._person_repo = PersonRepo()

        # Initialize device UUID
        self.init_device_uuid()

        # Initialize proxy layer
        async with mon.track_async(NodeName.MIOT_PROXY, "init"):
            self._miot_proxy = await MiotProxy.create_miot_proxy(
                uuid=self.device_uuid,
                redirect_uri="https://mico.api.mijia.tech/login_redirect",
                kv_repo=self._kv_repo,
                cloud_server=get_settings().miot.cloud_server,
            )

        # Initialize all services
        self._miot_service = MiotService(
            self._miot_proxy,
            self._person_repo,
        )
        self._person_service = PersonService(self._person_repo)
        self._home_profile_service = HomeProfileService(self._person_service)

        # Initialize rule module
        async with mon.track_async(NodeName.RULE_SERVICE, "init"):
            self._rule_service = await init_rule_service(self._miot_proxy)

        async with mon.track_async(NodeName.TERMINATE_EVALUATOR, "init"):
            self._terminate_evaluator = TerminateEvaluator(self._rule_service)
            self._terminate_evaluator.start()

        # Initialize perception module
        async with mon.track_async(NodeName.PERCEPTION_SERVICE, "init"):
            self._perception_service = await init_perception_module(
                self._miot_proxy, self._kv_repo
            )

        self._camera_service = CameraService(
            self._miot_service,
            self._perception_service,
        )
        self._initialize_live_stream_hub()

        self._task_service = TaskService(rule_service=self._rule_service)

        self._initialized = True

    def init_device_uuid(self):
        """Initialize device UUID"""
        device_uuid = self._kv_repo.get(SystemConfigKeys.DEVICE_UUID_KEY)
        if not device_uuid:
            device_uuid = uuid.uuid4().hex
            self._kv_repo.set(SystemConfigKeys.DEVICE_UUID_KEY, device_uuid)
        self.device_uuid = device_uuid

    # Service access properties
    @property
    def miot_service(self) -> MiotService:
        return self._miot_service

    @property
    def home_assistant_service(self):
        """Home Assistant service lazy singleton.

        Kept lazy so management API tests and disabled deployments do not have
        to initialize MIoT/perception first.
        """
        svc = getattr(self, "_home_assistant_service", None)
        if svc is None:
            from miloco.home_assistant.service import HomeAssistantService

            svc = HomeAssistantService()
            self._home_assistant_service = svc
        return svc

    @property
    def devices_service(self):
        """Unified MIoT + Home Assistant device service lazy singleton."""
        svc = getattr(self, "_devices_service", None)
        if svc is None:
            from miloco.devices.service import DevicesService

            svc = DevicesService(self.miot_service, self.home_assistant_service)
            self._devices_service = svc
        return svc

    @property
    def person_service(self) -> PersonService:
        return self._person_service

    @property
    def home_profile_service(self) -> HomeProfileService:
        return self._home_profile_service

    @property
    def rule_service(self) -> RuleService:
        return self._rule_service

    @property
    def perception_service(self) -> PerceptionService:
        return self._perception_service

    @property
    def camera_service(self) -> CameraService:
        return self._camera_service

    def _initialize_live_stream_hub(self) -> None:
        if getattr(self, "_live_stream_hub", None) is not None:
            return
        self._miot_live_backends: dict[tuple[str, int], _MiotLiveStreamBackend] = {}
        self._live_stream_camera_ids: set[str] = set()
        self._live_stream_hub = LiveStreamHub(self._resolve_live_stream)
        self._live_jpeg_stream_hub = LiveJpegStreamHub(self._resolve_live_stream)

    async def _resolve_live_stream(self, camera_id: str) -> LiveStreamSource:
        source = await self._camera_service.resolve_live_stream(camera_id)
        self._live_stream_camera_ids.add(camera_id)
        if source.source_type != "miot":
            return source
        key = (source.camera_id, source.channel)
        backend = self._miot_live_backends.get(key)
        if backend is None:
            from miloco.miot.ws import miot_video_stream_manager

            backend = _MiotLiveStreamBackend(
                miot_video_stream_manager, source.camera_id, source.channel
            )
            self._miot_live_backends[key] = backend
        return replace(source, backend=backend, input_codec="h264")

    @property
    def live_stream_hub(self) -> LiveStreamHub:
        return self._live_stream_hub

    @property
    def live_jpeg_stream_hub(self) -> LiveJpegStreamHub:
        return self._live_jpeg_stream_hub

    async def shutdown_live_streams(self) -> None:
        hub = getattr(self, "_live_stream_hub", None)
        jpeg_hub = getattr(self, "_live_jpeg_stream_hub", None)
        if hub is None and jpeg_hub is None:
            return
        camera_ids = set(getattr(self, "_live_stream_camera_ids", ()))
        if hub is not None:
            camera_ids.update(getattr(hub, "_feeds", ()))
        if jpeg_hub is not None:
            camera_ids.update(getattr(jpeg_hub, "_feeds", ()))
        for camera_id in camera_ids:
            if hub is not None:
                await hub.close_camera(camera_id)
            if jpeg_hub is not None:
                await jpeg_hub.close_camera(camera_id)
        backends = tuple(getattr(self, "_miot_live_backends", {}).values())
        await asyncio.gather(
            *(backend.aclose() for backend in backends), return_exceptions=True
        )
        self._live_stream_camera_ids.clear()

    @property
    def task_service(self) -> TaskService:
        return self._task_service

    # Repo layer access properties
    @property
    def kv_repo(self) -> KVRepo:
        return self._kv_repo

    @property
    def meaningful_events_dao(self):
        """meaningful_events DAO 懒加载单例.

        放在 Manager 上让 _persist_meaningful_event / events_service / cleanup loop
        共用同一实例.SQLiteConnector 单例,DAO 仅持引用,初始化零成本.
        """
        dao = getattr(self, "_meaningful_events_dao", None)
        if dao is None:
            from miloco.database.meaningful_events_dao import MeaningfulEventDao

            dao = MeaningfulEventDao()
            self._meaningful_events_dao = dao
        return dao

    @property
    def events_service(self):
        """events_service 懒加载单例;复用 self.meaningful_events_dao."""
        svc = getattr(self, "_events_service", None)
        if svc is None:
            from miloco.perception.events_service import EventsService

            svc = EventsService(self.meaningful_events_dao)
            self._events_service = svc
        return svc

    # Proxy layer access properties
    @property
    def miot_proxy(self) -> MiotProxy:
        return self._miot_proxy

    @property
    def onboarding_trigger(self):
        """onboarding 主动邀请触发器懒加载单例。

        依赖以可调用注入（同 DeviceWelcomeService 风格）：米家就绪 = 已授权
        （token 在 KV）且家庭启用集非空；成员 / 档案空判定分别走 person_service
        与 home_profile store（正式区）。
        """
        svc = getattr(self, "_onboarding_trigger", None)
        if svc is None:
            from miloco.database.kv_repo import AuthConfigKeys
            from miloco.home_profile import store as hp_store
            from miloco.home_profile.onboarding_trigger import OnboardingTriggerService
            from miloco.miot.filter import allowed_home_ids

            kv = self._kv_repo
            svc = OnboardingTriggerService(
                kv_repo=kv,
                is_miot_ready=lambda: bool(kv.get(AuthConfigKeys.MIOT_TOKEN_INFO_KEY))
                and bool(allowed_home_ids(kv)),
                has_persons=lambda: bool(self._person_service.list_persons()),
                has_profile_entries=lambda: bool(hp_store.load_profile().entries),
            )
            self._onboarding_trigger = svc
        return svc

    # 主动注册:registration session manager lazy 单例
    # 进程内单一实例,管理 pending dict + commit / sessions / rollback。
    @property
    def register_session_manager(self):
        rsm = getattr(self, "_register_session_manager", None)
        if rsm is None:
            from miloco.perception.engine.identity.config_loader import (
                resolve_library_root,
            )
            from miloco.perception.engine.identity.library import IdentityLibrary
            from miloco.perception.engine.identity.registration_session import (
                RegistrationSessionManager,
            )
            lib = IdentityLibrary(resolve_library_root())
            rsm = RegistrationSessionManager(lib)
            self._register_session_manager = rsm
        return rsm


# Global singleton instance
manager_instance: Manager | None = None


def get_manager():
    """Get Manager singleton instance"""
    global manager_instance
    if manager_instance is None:
        manager_instance = Manager()
    return manager_instance
