# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Integration tests for `miot.mips_local.MipsLocalClient`.

A fake paho Client is injected via the `client_factory` hook — no real broker
or mTLS. The fake records subscribe/publish/unsubscribe and exposes `fire_*`
helpers to drive paho callbacks. Covers:

  - CONNACK success → base topics subscribed; CONNACK reject → MipsConnectionError
  - RPC request/reply matched by mid (get_prop / getDevList parse)
  - RPC timeout → CODE_TIMEOUT result, request map cleaned
  - reconnect re-subscribes registered broadcasts
  - property / event push dispatched to handlers (bad payloads dropped)
  - deinit resolves in-flight requests as result-unknown instead of hanging
  - publish rc != SUCCESS → MipsConnectionError (definitely-not-sent)
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from miot.error import MIoTErrorCode
from miot.mips_local import MipsLocalClient, _MipsMessage
from miot.types import (
    MipsConnectionError,
    MipsSubscribeRejectedError,
    MipsSubscribeTimeoutError,
)
from paho.mqtt.enums import MQTTErrorCode

from miot import mips_local as mips_local_mod

_LOGGER = logging.getLogger(__name__)

# 无真实 broker / mTLS / 网络：paho client 由 client_factory 注入 fake。
pytestmark = pytest.mark.unit

_DID = "1899"


class _FakeReasonCode:
    def __init__(self, value: int) -> None:
        self.value = value


class _FakePubInfo:
    def __init__(self, rc) -> None:
        self.rc = rc


class _FakeMqtt:
    """Subset of paho Client used by MipsLocalClient."""

    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self.publish_rc = MQTTErrorCode.MQTT_ERR_SUCCESS
        self.connect_host = None
        self.connect_port = None
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.published: list[tuple[str, bytes]] = []
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None
        self.on_subscribe = None
        # 回包行为:默认照实回一个 Granted QoS 2;可改成拒码或不回(模拟超时)。
        self.suback_rcs: list[int] = [0x02]
        self.silent_suback = False
        self.sub_rc = MQTTErrorCode.MQTT_ERR_SUCCESS
        self._next_mid = 100

    # -- paho API used by mips_local
    def tls_set(self, **kw):
        pass

    def tls_insecure_set(self, v):
        pass

    def reconnect_delay_set(self, min_delay, max_delay):
        pass

    def connect(self, host, port, keepalive, clean_start):
        self.connect_host = host
        self.connect_port = port

    def disconnect(self):
        pass

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def subscribe(self, topic, qos):
        self.subscribed.append(topic)
        mid = self._next_mid
        self._next_mid += 1
        if self.sub_rc == MQTTErrorCode.MQTT_ERR_SUCCESS and not self.silent_suback:
            # 真 SUBACK 只能晚于 subscribe() 返回(网络往返),所以也排到下一个
            # loop tick —— 在那之前调用方才刚把在等的 future 登记上。
            codes = [_FakeReasonCode(c) for c in self.suback_rcs]
            try:
                asyncio.get_running_loop().call_soon(self._deliver_suback, mid, codes)
            except RuntimeError:  # 没有 loop(不该发生):就地回
                self._deliver_suback(mid, codes)
        return self.sub_rc, mid

    def _deliver_suback(self, mid, codes):
        if self.on_subscribe:
            self.on_subscribe(self, None, mid, codes, None)

    def unsubscribe(self, topic):
        self.unsubscribed.append(topic)
        return MQTTErrorCode.MQTT_ERR_SUCCESS, 1

    def publish(self, topic, payload, qos):
        self.published.append((topic, payload))
        # 真 paho 返回 MQTTMessageInfo,mips_local 现在会检查 .rc(未连接时 paho 不抛
        # 异常只把 rc 置成 MQTT_ERR_NO_CONN,不查就会把"没发出去"当成超时)。
        return _FakePubInfo(self.publish_rc)

    # -- test drivers
    def fire_connect(self, rc=0):
        self.on_connect(self, None, None, _FakeReasonCode(rc), None)

    def fire_disconnect(self, rc=0):
        self.on_disconnect(self, None, None, _FakeReasonCode(rc), None)

    def fire_message(self, topic, payload: bytes):
        class _Msg:
            def __init__(self, t, p):
                self.topic = t
                self.payload = p

        self.on_message(self, None, _Msg(topic, payload))


def _make(did=_DID):
    holder: dict = {}

    def factory(client_id):
        holder["c"] = _FakeMqtt(client_id)
        return holder["c"]

    client = MipsLocalClient(
        did=did,
        host="10.0.0.9",
        group_id="g1",
        ca_file="ca",
        cert_file="crt",
        key_file="key",
        client_factory=factory,
    )
    return client, holder


async def _connect(client, holder, rc=0):
    async def drive():
        for _ in range(50):
            if "c" in holder and holder["c"].connect_host is not None:
                break
            await asyncio.sleep(0.005)
        holder["c"].fire_connect(rc=rc)

    await asyncio.gather(client.init_async(), drive())
    return holder["c"]


async def _reply(client, fake, result_obj, *, timeout=1.0):
    """Wait for a pending request, then fire its reply on {did}/reply."""
    deadline = asyncio.get_event_loop().time() + timeout
    while not client._request_map and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.005)
    assert client._request_map, "no pending request appeared"
    mid = int(next(iter(client._request_map.keys())))
    packed = _MipsMessage.pack(mid=mid, payload=json.dumps(result_obj))
    fake.fire_message(f"{_DID}/reply", packed)


# ============================================================================


@pytest.mark.asyncio
async def test_connack_success_subscribes_base_topics():
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        assert client.is_connected
        assert f"{_DID}/#" in fake.subscribed
        assert "master/appMsg/devListChange" in fake.subscribed
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_connack_rejected_raises():
    client, holder = _make()
    with pytest.raises(MipsConnectionError):
        await _connect(client, holder, rc=0x87)  # Not authorized
    assert not client.is_connected


@pytest.mark.asyncio
async def test_get_prop_request_reply_by_mid():
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        task = asyncio.create_task(client.get_prop_async(_DID, 2, 4))
        await _reply(client, fake, {"value": True})
        assert await task == {"value": True, "code": 0}
        # published to master/proxy/get
        assert any(t == "master/proxy/get" for t, _ in fake.published)
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_get_dev_list_parses_fields():
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        task = asyncio.create_task(client.get_dev_list_async())
        await _reply(
            client,
            fake,
            {
                "devList": {
                    "d1": {
                        "online": True,
                        "specV2Access": True,
                        "pushAvailable": False,
                    }
                }
            },
        )
        devs = await task
        assert devs["d1"]["online"] is True
        assert devs["d1"]["specv2_access"] is True  # renamed from specV2Access
        assert devs["d1"]["push_available"] is False
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_rpc_timeout_returns_timeout_code_and_cleans_map():
    import miot.mips_local as ml

    orig = ml.MIPS_LOCAL_RPC_TIMEOUT
    ml.MIPS_LOCAL_RPC_TIMEOUT = 0.15
    client, holder = _make()
    await _connect(client, holder)
    try:
        r = await client.set_prop_async(_DID, 2, 4, True)  # no reply → timeout
        assert r["code"] == MIoTErrorCode.CODE_TIMEOUT.value
        assert client._request_map == {}  # timer popped it
    finally:
        ml.MIPS_LOCAL_RPC_TIMEOUT = orig
        await client.deinit_async()


@pytest.mark.asyncio
async def test_reconnect_resubscribes_broadcasts():
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        await client.sub_device_props_async("d2", lambda _e: None)
        assert "master/appMsg/notify/iot/d2/property/#" in fake.subscribed
        before = len(fake.subscribed)
        fake.fire_disconnect(rc=1)
        fake.fire_connect(rc=0)  # paho auto-reconnect → on_connect again
        resubbed = fake.subscribed[before:]
        assert "master/appMsg/notify/iot/d2/property/#" in resubbed
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_prop_push_dispatched_and_bad_payload_dropped():
    client, holder = _make()
    fake = await _connect(client, holder)
    got: list = []
    try:
        await client.sub_device_props_async("d2", got.append)
        # valid push on {did}/appMsg/notify/iot/d2/property/2.4
        fake.fire_message(
            f"{_DID}/appMsg/notify/iot/d2/property/2.4",
            _MipsMessage.pack(
                mid=1,
                payload=json.dumps(
                    {"did": "d2", "siid": 2, "piid": 4, "value": False}
                ),
            ),
        )
        # 缺必填键 → 丢掉（不产事件）
        fake.fire_message(
            f"{_DID}/appMsg/notify/iot/d2/property/2.4",
            _MipsMessage.pack(mid=2, payload=json.dumps({"did": "d2"})),
        )
        for _ in range(10):
            await asyncio.sleep(0)
        assert len(got) == 1
        assert [(c.siid, c.piid, c.value) for c in got[0].changes] == [(2, 4, False)]
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_sub_device_props_decodes_to_the_canonical_event():
    """本地推送解码成正典 MIoTDevicePropsEvent —— 与云端那条**同形**。

    两边的形状实打实不同：本地 topic 用点号（``property/2.4``）、载荷是单条
    ``{did,siid,piid,value}``；云端用斜杠（``properties_changed/2/4``）、
    ``params`` 可以是数组、一条消息带多条变更。归一到同一个类型之后，
    ``IotPushWriter`` 不必知道这条推送来自哪条通道。
    """
    client, holder = _make()
    fake = await _connect(client, holder)
    got: list = []
    try:
        await client.sub_device_props_async("d2", got.append)
        fake.fire_message(
            f"{_DID}/appMsg/notify/iot/d2/property/2.4",
            _MipsMessage.pack(
                mid=1,
                payload=json.dumps(
                    {"did": "d2", "siid": 2, "piid": 4, "value": False}
                ),
            ),
        )
        for _ in range(10):
            await asyncio.sleep(0)
    finally:
        await client.deinit_async()

    assert len(got) == 1
    event = got[0]
    assert event.did == "d2"
    assert [(c.siid, c.piid, c.value) for c in event.changes] == [(2, 4, False)]
    assert event.timestamp_ms > 0
    assert event.raw["value"] is False


@pytest.mark.asyncio
async def test_has_props_sub_reflects_this_instances_registration_table():
    """``has_props_sub`` 要如实回答本实例有没有这条注册 —— 中枢靠它路由退订。

    设备改嫁网关后设备表指向新的那台、注册却还在旧实例上，两者只能靠这个查询分开。
    报错就会去退一个没有注册的实例：扑空、旧注册永远留着，重连时还被原样重放。
    """
    client, holder = _make()
    await _connect(client, holder)
    try:
        assert client.has_props_sub("d2") is False

        await client.sub_device_props_async("d2", lambda _e: None)
        assert client.has_props_sub("d2") is True

        client.unsub_device_props("d2")
        assert client.has_props_sub("d2") is False

        await client.sub_device_props_async("d3", lambda _e: None)
        assert client.has_props_sub("d2") is False, "别的设备的注册不算数"
        assert client.has_props_sub("d3") is True
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_a_gateway_rejection_is_not_recorded_as_a_subscription():
    """网关在 SUBACK 里拒绝 → 抛，且注册表里不留痕。

    这是「把 subscribe() 调用当成功」最要命的一种：请求发出去了、rc 也是成功，
    拒绝只体现在后续 SUBACK 里。若记成订上了，上层会把还在工作的云端订阅退掉，
    而本地一条都没有 —— 推送静默丢失，幂等早退还让重试进不来。
    """
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        fake.suback_rcs = [0x87]  # Not authorized
        with pytest.raises(MipsSubscribeRejectedError):
            await client.sub_device_props_async("d2", lambda _e: None)

        assert client.has_props_sub("d2") is False, "被拒的注册不许留在表里"
        # 表里没有 → 重试会真的重发，不会被幂等早退挡掉
        before = len(fake.subscribed)
        fake.suback_rcs = [0x02]
        await client.sub_device_props_async("d2", lambda _e: None)
        assert len(fake.subscribed) > before
        assert client.has_props_sub("d2") is True
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_a_subscribe_that_never_left_the_host_is_reported_as_failed():
    """连接恰在 is_connected 检查与 subscribe() 之间断掉 → paho 只返 rc，不抛。

    与 ``__mips_publish`` 同一个坑：不查 rc 就会把「根本没发出去」当成「发出去了」。
    """
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        fake.sub_rc = MQTTErrorCode.MQTT_ERR_NO_CONN
        with pytest.raises(MipsConnectionError):
            await client.sub_device_props_async("d2", lambda _e: None)
        assert client.has_props_sub("d2") is False
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_a_missing_suback_times_out_and_is_not_recorded(monkeypatch):
    """网关不回 SUBACK → 超时失败、注册摘掉。实测网关是回的，所以这条是活性闸门。"""
    monkeypatch.setattr(mips_local_mod, "MIPS_LOCAL_SUBSCRIBE_TIMEOUT", 0.05)
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        fake.silent_suback = True
        with pytest.raises(MipsSubscribeTimeoutError):
            await client.sub_device_props_async("d2", lambda _e: None)
        assert client.has_props_sub("d2") is False
        assert not client._pending_subacks, "在等的 future 不许漏在表里"
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_a_disconnect_fails_the_pending_suback_without_waiting_for_the_timeout():
    """断连时在等的 SUBACK 要立刻失败：回包不会来了，干等满超时等于把「明确失败」

    拖成「疑似超时」，而调用方正等着这个结果决定记不记账。
    """
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        fake.silent_suback = True
        waiter = asyncio.create_task(
            client.sub_device_props_async("d2", lambda _e: None)
        )
        for _ in range(10):
            await asyncio.sleep(0)
        assert not waiter.done(), "还在等 SUBACK"

        fake.fire_disconnect(rc=1)
        with pytest.raises(MipsConnectionError):
            await asyncio.wait_for(waiter, timeout=1.0)
        assert client.has_props_sub("d2") is False
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_a_cancelled_subscription_does_not_leave_a_registration():
    """在等 SUBACK 时被取消（deinit / 重绑会 cancel 后台任务）也要回滚。

    上层镜像压根没记这次订阅（提交块没跑），留着那条键就是「表说订了、镜像说没订」，
    而 has_props_sub 拿它回答「注册落在哪台」，会连带影响搬运判定。
    """
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        fake.silent_suback = True
        task = asyncio.create_task(
            client.sub_device_props_async("d2", lambda _e: None)
        )
        for _ in range(10):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.has_props_sub("d2") is False
        assert not client._pending_subacks
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_a_rejected_replayed_subscribe_does_not_leave_a_lying_key():
    """重连重放是 fire-and-forget（没人等它的 SUBACK），但网关拒绝时照样要摘键。

    否则「表里只放已确认的注册」这句在重放这条路上就是假的。
    """
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        await client.sub_device_props_async("d2", lambda _e: None)
        assert client.has_props_sub("d2") is True

        fake.suback_rcs = [0x87]  # 重连后网关改主意了（ACL 撤销等）
        fake.fire_connect(rc=0)  # _on_connect 原样重放 _broadcasts
        for _ in range(10):
            await asyncio.sleep(0)

        assert client.has_props_sub("d2") is False
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_a_concurrent_subscribe_shares_the_first_ones_failure(monkeypatch):
    """同一台设备被两路并发订阅、首订确认失败 → 后来者**不能**返回成功。

    后来者若乐观返回成功，上层会记下「推送走本地」，而首订失败时把注册回滚掉 ——
    镜像说订在本地、注册表里一条都没有，推送静默丢失，自愈要等下一次 hub 事件。
    """
    monkeypatch.setattr(mips_local_mod, "MIPS_LOCAL_SUBSCRIBE_TIMEOUT", 0.05)
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        fake.silent_suback = True  # 首订永远等不到 SUBACK
        first = asyncio.create_task(
            client.sub_device_props_async("d2", lambda _e: None)
        )
        for _ in range(10):
            await asyncio.sleep(0)  # 让首订进入等待（键已在表里、未确认）

        with pytest.raises(MipsSubscribeTimeoutError):
            await client.sub_device_props_async("d2", lambda _e: None)

        with pytest.raises(MipsSubscribeTimeoutError):
            await first
        assert client.has_props_sub("d2") is False
        assert not client._props_confirms
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_a_prop_push_whose_payload_did_differs_is_dropped():
    """载荷里的 did 与订阅的 did 不一致 → 丢掉。

    topic 已经按 did 精订，载荷里又重复一份，这是白送的交叉校验：不一致意味着
    网关把一台设备的值发到了另一台的路径上，收下来就是串台。
    """
    client, holder = _make()
    fake = await _connect(client, holder)
    got: list = []
    try:
        await client.sub_device_props_async("d2", got.append)
        fake.fire_message(
            f"{_DID}/appMsg/notify/iot/d2/property/2.4",
            _MipsMessage.pack(
                mid=1,
                payload=json.dumps(
                    {"did": "d9", "siid": 2, "piid": 4, "value": False}
                ),
            ),
        )
        for _ in range(10):
            await asyncio.sleep(0)
    finally:
        await client.deinit_async()

    assert got == []


@pytest.mark.asyncio
async def test_event_push_dispatched_with_arguments_default():
    client, holder = _make()
    fake = await _connect(client, holder)
    got: list[dict] = []
    try:
        client.sub_event(did="d2", handler=lambda m, c: got.append(m))
        ev = {"did": "d2", "siid": 8, "eiid": 10}
        fake.fire_message(
            f"{_DID}/appMsg/notify/iot/d2/event/8.10",
            _MipsMessage.pack(mid=1, payload=json.dumps(ev)),
        )
        for _ in range(10):
            await asyncio.sleep(0)
        assert len(got) == 1
        assert got[0]["eiid"] == 10
        assert got[0]["arguments"] == []  # defaulted
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_deinit_resolves_inflight_request_as_result_unknown():
    """deinit 打断在途请求 → 收尾成 **-10006(结果未知)**,不是抛异常。

    future 只在 publish 成功之后才进 _request_map,所以"在途"必然意味着报文已经发给
    网关。路由层把异常一律当"明确未执行"转云端重发,若这里抛 MipsConnectionError,
    「连接被替换/拆除」(切家庭、切号、20s 重连 sweep 撞上 ≤5s 应答窗口)就会让非幂等
    动作走两遍 —— 正是本 PR 的核心承诺要消灭的形态。
    """
    import miot.mips_local as ml

    orig = ml.MIPS_LOCAL_RPC_TIMEOUT
    ml.MIPS_LOCAL_RPC_TIMEOUT = 30  # long, so deinit is what resolves it
    client, holder = _make()
    await _connect(client, holder)
    try:
        task = asyncio.create_task(client.action_async(_DID, 2, 1, []))
        # wait until the request is registered
        for _ in range(50):
            if client._request_map:
                break
            await asyncio.sleep(0.005)
        assert client._request_map
        await client.deinit_async()
        res = await task
        assert res["code"] == MIoTErrorCode.CODE_TIMEOUT.value
    finally:
        ml.MIPS_LOCAL_RPC_TIMEOUT = orig


@pytest.mark.asyncio
async def test_publish_failure_raises_connection_error():
    """publish 的 rc 非 SUCCESS = 报文没进发送队列 = **明确没发出去** → 抛异常。

    不查 rc 的话调用方会干等满超时拿 -10006,而 -10006 被上层按"可能已送达"处置
    (set/action 不回落云端 + 冷却)——一条根本没出本机的指令被当成结果未知,
    用户操作无声丢失。抛异常才让路由层走"安全重发云端"。
    """
    client, holder = _make()
    fake = await _connect(client, holder)
    fake.publish_rc = MQTTErrorCode.MQTT_ERR_NO_CONN
    with pytest.raises(MipsConnectionError):
        await client.action_async(_DID, 2, 1, [])
    # 失败后不能留下在途条目/定时器,否则下一轮 deinit 会给它编一个假的"结果未知"。
    assert client._request_map == {}
    await client.deinit_async()


@pytest.mark.asyncio
async def test_unrecognised_reply_shape_returns_result_unknown():
    """网关回了包但结构不认识 → -10041(结果未知),**不是** -10004。

    走真实回包路径而非 fake 网关:上层 client.py 靠这个码决定"不重发云端",
    如果这里退回 -10004,双发防护就失效了(-10004 语义是"两个通道都没结果")。
    """
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        task = asyncio.create_task(client.set_prop_async(_DID, 2, 4, True))
        # 既没有 result 也没有 error,且不带 code
        await _reply(client, fake, {"unexpected": "shape"})
        r = await task
        assert r["code"] == MIoTErrorCode.CODE_MIPS_RESULT_UNKNOWN.value
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_reply_carrying_code_is_passed_through():
    """回包自带 code 时原样上抛,不被重新贴成 -10004/-10041。

    这是 -10040(应答非合法 JSON,由 __request_async 自产)能真正到达 client.py
    路由表的前提 —— 此前它在这里被覆写,导致路由表里那条分支永不命中。
    """
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        task = asyncio.create_task(client.set_prop_async(_DID, 2, 4, True))
        await _reply(
            client, fake,
            {"code": MIoTErrorCode.CODE_MIPS_INVALID_RESULT.value, "message": "x"},
        )
        r = await task
        assert r["code"] == MIoTErrorCode.CODE_MIPS_INVALID_RESULT.value
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_action_unrecognised_reply_shape_returns_result_unknown():
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        task = asyncio.create_task(client.action_async(_DID, 2, 1, []))
        await _reply(client, fake, {"unexpected": "shape"})
        r = await task
        assert r["code"] == MIoTErrorCode.CODE_MIPS_RESULT_UNKNOWN.value
    finally:
        await client.deinit_async()


# ─── 相邻的畸形回包形状:result 类型错/error 类型错/payload 非对象 ────────────
#
# 上面两条只覆盖了「是 dict 但既没有 result 也没有 error 键」这一种。相邻的几种
# 同样"网关已回包但结构不认识"的形状此前会抛异常(KeyError/TypeError)或退化成
# 非 dict,两条路都被 client.py 判成"肯定没执行"而云端重发 —— 同一条非幂等指令
# 在设备上执行两次,正是这整套分流机制要防的事。


@pytest.mark.asyncio
async def test_set_prop_result_not_a_list_returns_result_unknown():
    """result 是对象而非单元素数组:旧代码在下标访问 result[0] 处抛 KeyError。"""
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        task = asyncio.create_task(client.set_prop_async(_DID, 2, 4, True))
        await _reply(client, fake, {"result": {"code": 0}})
        r = await task  # 不能抛异常
        assert r["code"] == MIoTErrorCode.CODE_MIPS_RESULT_UNKNOWN.value
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_set_prop_error_not_a_dict_returns_result_unknown():
    """error 是字符串而非对象:旧代码原样返回字符串,上层 isinstance 判 code=None
    →"肯定没执行"→ 云端重发,且整条链路不会打一行异常日志。"""
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        task = asyncio.create_task(client.set_prop_async(_DID, 2, 4, True))
        await _reply(client, fake, {"error": "denied"})
        r = await task
        assert isinstance(r, dict)  # 不能是裸字符串
        assert r["code"] == MIoTErrorCode.CODE_MIPS_RESULT_UNKNOWN.value
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_set_prop_non_object_payload_returns_result_unknown():
    """payload 是合法 JSON 但非对象(如裸整数):旧代码在 "result" in 5 处抛
    TypeError。"""
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        task = asyncio.create_task(client.set_prop_async(_DID, 2, 4, True))
        await _reply(client, fake, 5)
        r = await task
        assert r["code"] == MIoTErrorCode.CODE_MIPS_RESULT_UNKNOWN.value
    finally:
        await client.deinit_async()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_action_result_is_canonical_and_keeps_top_level_exe_time():
    """实测网关回包走一遍 action_async,必须出正典形状。

    下面这份 payload 是 2026-10-12 从真机抓的(人在传感器Pro 的
    get-tracking-info)。要点:**网关把 ``exe_time`` 放在回包顶层、和 ``result``
    平级**,而旧代码只做 ``result_obj.get("result")`` —— 顶层字段连同一起被丢掉,
    所以本地这条通道看起来"没有 exe_time"。实测:50。
    """
    tracking = '{"l":182,"p":[[2831,325,163,489,21202159]],"mv":0,"mn":1,"ota":0,"rp":1}'
    gateway_payload = {
        "id": 1778597798,
        "result": {"code": 0, "out": [{"piid": 7, "value": tracking}]},
        "exe_time": 50,
    }
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        task = asyncio.create_task(client.action_async(_DID, 3, 4, []))
        await _reply(client, fake, gateway_payload)
        r = await task

        assert r["code"] == 0
        assert r["source"] == "local_central"
        assert (r["did"], r["siid"], r["aiid"]) == (_DID, 3, 4)
        # 本地形态的 piid 是权威值,原样保留
        assert r["out"] == [{"piid": 7, "value": tracking}]
        assert r["exe_time"] == 50
        # 往返是这里自己量的
        assert isinstance(r["latency"], int) and r["latency"] >= 0
        assert r["net_cost"] == r["latency"] - 50
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_action_result_not_a_dict_returns_result_unknown():
    """action 的 result 是非 dict(如裸整数):旧代码在 "code" in result_obj["result"]
    处抛 TypeError。"""
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        task = asyncio.create_task(client.action_async(_DID, 2, 1, []))
        await _reply(client, fake, {"result": 5})
        r = await task
        assert r["code"] == MIoTErrorCode.CODE_MIPS_RESULT_UNKNOWN.value
    finally:
        await client.deinit_async()


@pytest.mark.asyncio
async def test_action_error_not_a_dict_returns_result_unknown():
    """action 的 error 是字符串而非对象,与 set_prop 同理。"""
    client, holder = _make()
    fake = await _connect(client, holder)
    try:
        task = asyncio.create_task(client.action_async(_DID, 2, 1, []))
        await _reply(client, fake, {"error": "denied"})
        r = await task
        assert isinstance(r, dict)
        assert r["code"] == MIoTErrorCode.CODE_MIPS_RESULT_UNKNOWN.value
    finally:
        await client.deinit_async()
