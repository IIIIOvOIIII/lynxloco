# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""属性推送的**来源选择**：每台设备选一条通道，能力翻转时搬过去。

不变量是「任一时刻每台设备只有一个活跃源」，落地靠**互斥**而不是「两条都订 +
去重」：两条都订时本地往返比云端短（实测同一台设备 90ms vs 164ms），同一条属性
在两次变化间隔小于这个差时，云端那条**旧值**会盖在本地新值之后 —— 而两边都不带
设备时间戳，容器无从仲裁。去重只去得掉同值，去不掉乱序。

判据用网关的 ``push_available``（``can_push``），**不是**控制那条用的
``specv2_access``：两套标志相互独立，所以同一台设备的控制源和推送源可以不是同一条。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from miot.action_result import SOURCE_CLOUD, SOURCE_LOCAL_CENTRAL
from miot.client import MIoTClient
from miot.types import MipsConnectionError

from miot import client as client_mod

pytestmark = pytest.mark.unit


class _FakeHub:
    """够用的中枢替身：两个能力标志 + 网关身份 + 每台实例的注册表，全记进同一条流水。

    ``groups`` 答「设备表现在把 did 指给哪台网关」，``regs`` 是**每台实例各一张**
    注册表（对应真实的 ``MipsLocalClient._broadcasts``）—— 分成两份、且按实例分表，
    才能覆盖「设备在两台都活着的网关之间迁移」：迁移后设备表指向新的，注册却还在
    旧实例上，而退订必须按后者路由。
    """

    def __init__(
        self, log: list, *, pushable=(), controllable=(), groups=None
    ):
        self.enabled = True
        self.sub_ok = True
        self._log = log
        self._pushable = set(pushable)
        self._controllable = set(controllable)
        # 没点名归属的设备默认都挂在同一台网关上 —— 真实 hub 的 group_id 不会是
        # None，而 None 在这套接口里是「没有注册」的哨兵，混用会把两者搅在一起。
        self.groups = dict(groups or {})
        self.regs: dict = {}  # group_id -> {did, …}

    def _group_of(self, did: str) -> str:
        return self.groups.get(did, "gA")

    def can_push(self, did: str) -> bool:
        return did in self._pushable

    def can_control(self, did: str) -> bool:
        return did in self._controllable

    def local_device(self, did: str):
        return {"group_id": self._group_of(did)}

    def props_sub_groups(self, did: str) -> set:
        return {gid for gid, dids in self.regs.items() if did in dids}

    async def sub_device_props_async(self, did, handler) -> None:
        if not self.sub_ok:
            raise MipsConnectionError("fake hub: subscribe refused")
        self._log.append(("local+", did))
        # 与真实实现一致：sub 按设备表当前指向的那台路由
        self.regs.setdefault(self._group_of(did), set()).add(did)

    def unsub_device_props(self, did, keep=None) -> bool:
        # 真实实现逐个实例问、有就摘；keep 给定的那台留着
        held = sorted(self.props_sub_groups(did) - ({keep} if keep else set()))
        if not held:
            return False
        for group_id in held:
            self.regs[group_id].discard(did)
            self._log.append(("local-", did, group_id))
        return True


def _client(hub=None, *, connected=True):
    """``hub`` 为 None 时模拟「没接本地中枢」——这是本轮之前的行为。"""
    log = hub._log if hub is not None else []
    client = object.__new__(MIoTClient)
    client._props_sub_dids = set()
    client._props_sub_sources = {}
    client._props_source_lock = asyncio.Lock()
    client._callback_device_props_changed = None
    client._callback_dev_list_changed = None
    client._central_hub = hub

    async def cloud_sub(did, handler):
        log.append(("cloud+", did))

    async def cloud_unsub(did):
        log.append(("cloud-", did))

    client._mips_cloud = SimpleNamespace(
        is_connected=connected,
        sub_device_props_async=AsyncMock(side_effect=cloud_sub),
        unsub_device_props_async=AsyncMock(side_effect=cloud_unsub),
    )
    return client, log


async def test_a_pushable_device_subscribes_locally():
    """网关说这台设备能推 → 走本地，云端一个字节都不发。"""
    client, log = _client(_FakeHub([], pushable={"d1"}))

    await client.sub_device_props_async("d1")

    assert log == [("local+", "d1")]
    assert client._props_sub_sources["d1"] == SOURCE_LOCAL_CENTRAL
    client._mips_cloud.sub_device_props_async.assert_not_awaited()


async def test_a_device_without_push_available_stays_on_cloud():
    """在网关设备表里但 push_available=false → 留在云端。"""
    client, log = _client(_FakeHub([], pushable=set()))

    await client.sub_device_props_async("d1")

    assert log == [("cloud+", "d1")]
    assert client._props_sub_sources["d1"] == SOURCE_CLOUD


async def test_without_a_hub_everything_stays_on_cloud():
    """没接中枢（或中枢关着）时行为与本轮之前完全一致。"""
    client, log = _client(None)

    await client.sub_device_props_async("d1")

    assert log == [("cloud+", "d1")]


async def test_the_push_source_does_not_consult_the_control_flag():
    """能推但不可本地控 → 推送走本地。

    选订阅源用 push_available、选控制源用 specv2_access，是两套独立标志。这条
    用例钉的就是「推送判断里不许出现 can_control」—— 一旦有人把两个谓词合并，
    这台设备的推送会被无谓地赶去云端（或者反过来，把不可控的当可控去发指令）。
    """
    hub = _FakeHub([], pushable={"d1"}, controllable=set())
    client, log = _client(hub)
    assert hub.can_control("d1") is False

    await client.sub_device_props_async("d1")

    assert log == [("local+", "d1")]


async def test_a_flip_subscribes_the_new_source_before_dropping_the_old():
    """换源次序：先订新、再退旧。

    重叠窗口里同一条变更被投两次，值相同 → 容器判等后零事件；反过来先退后订，
    窗口里落地的变更两条通道都收不到，容器停在旧值。静默丢比重复难查得多，
    所以这个次序是有代价权衡的，不是随手写的。
    """
    hub = _FakeHub([], pushable={"d1"})
    client, log = _client(hub)
    await client.sub_device_props_async("d1")

    hub._pushable = set()  # 网关不再推（设备下线 / 网关断连）
    await client._sync_props_sources_async()

    assert log == [("local+", "d1"), ("cloud+", "d1"), ("local-", "d1", "gA")]
    assert client._props_sub_sources["d1"] == SOURCE_CLOUD


async def test_a_flip_back_to_local_reuses_the_new_gateway_client():
    """网关卡死又回来：新实例上没有旧订阅，得按当前 client 重订。

    这条覆盖的是 `CentralHubManager.__drop_client` 之后重连 sweep **新建** 一个
    MipsLocalClient 的路径 —— 新实例的 `_broadcasts` 是空的，没有任何人会补。
    来源镜像按 did 存、重订时现取 client，所以这里天然补上。
    """
    hub = _FakeHub([], pushable={"d1"})
    client, log = _client(hub)
    await client.sub_device_props_async("d1")
    hub._pushable = set()
    await client._sync_props_sources_async()

    hub._pushable = {"d1"}  # 网关回来了
    await client._sync_props_sources_async()

    assert log == [
        ("local+", "d1"),
        ("cloud+", "d1"),
        ("local-", "d1", "gA"),
        ("local+", "d1"),
        ("cloud-", "d1"),
    ]
    assert client._props_sub_sources["d1"] == SOURCE_LOCAL_CENTRAL


async def test_an_unavailable_new_source_keeps_the_old_one():
    """订不上新的就留在旧的：退回「两条都没有」是净损失。"""
    hub = _FakeHub([], pushable={"d1"})
    client, log = _client(hub)
    await client.sub_device_props_async("d1")
    hub._pushable = set()

    client._mips_cloud.is_connected = False  # 云端也订不上
    await client._sync_props_sources_async()

    assert log == [("local+", "d1")]
    assert client._props_sub_sources["d1"] == SOURCE_LOCAL_CENTRAL
    assert client._props_sub_dids == {"d1"}


async def test_unsubscribe_targets_the_channel_it_was_subscribed_on():
    """退订要退**当初订的那条**：来源镜像丢了就会退错通道，本地订阅永远留着。"""
    hub = _FakeHub([], pushable={"d1"})
    client, log = _client(hub)

    await client.sub_device_props_async("d1")
    await client.unsub_device_props_async("d1")

    assert log == [("local+", "d1"), ("local-", "d1", "gA")]
    assert client._props_sub_dids == set()
    assert client._props_sub_sources == {}
    client._mips_cloud.unsub_device_props_async.assert_not_awaited()


async def test_a_cloud_subscribed_device_unsubscribes_on_cloud():
    """方向反过来同样要退对：不能因为「中枢在」就默认走本地。"""
    hub = _FakeHub([], pushable=set())
    client, log = _client(hub)

    await client.sub_device_props_async("d1")
    await client.unsub_device_props_async("d1")

    assert log == [("cloud+", "d1"), ("cloud-", "d1")]


async def test_the_dev_list_hook_reroutes_before_notifying_the_business_layer():
    """设备表变化的处理次序：先搬来源，再转给业务层。

    反过来的话业务层那次重拉会看到还没收敛的来源镜像（设备已经能推了，
    推送却还挂在云端），要多等一轮变化才纠正。
    """
    hub = _FakeHub([], pushable=set())
    client, log = _client(hub)
    await client.sub_device_props_async("d1")

    seen: list = []

    async def on_change(added, removed):
        seen.append((added, removed))
        log.append(("callback", "d1"))

    client._callback_dev_list_changed = on_change
    hub._pushable = {"d1"}  # 设备上线且网关能推了

    await client._on_dev_list_changed_from_hub(["d1"], [])

    assert log == [("cloud+", "d1"), ("local+", "d1"), ("cloud-", "d1"), ("callback", "d1")]
    assert seen == [(["d1"], [])]


async def test_a_business_unsubscribe_waits_for_an_in_flight_move():
    """搬运在飞时业务层的镜像改动**排队等它落定**，不能交错进去。

    交错的话 `__try_subscribe` 末尾那句无条件的镜像写回会把业务刚记下的来源改谎：
    两条通道同时活着、镜像只记一条，后续退订退不干净（另一条成孤儿）。业务层同形的
    窗口用 generation 提交作废防，这条 SDK 搬运路径靠的是同一把锁。

    注意锁**只罩镜像改动**（这里就是 `pop`），网络退订在锁外 —— 见下一条用例。
    """
    log: list = []
    client, log = _client(_FakeHub(log, pushable={"d1"}))
    await client.sub_device_props_async("d1")  # 本地

    hub = client._central_hub
    inner = client._mips_cloud.sub_device_props_async
    in_flight = asyncio.Event()
    gate = asyncio.Event()

    async def slow_cloud_sub(did, handler):
        in_flight.set()
        await gate.wait()  # 把搬运按在「订新源」这一步上
        await inner(did, handler)

    client._mips_cloud.sub_device_props_async = AsyncMock(side_effect=slow_cloud_sub)
    hub._pushable = set()  # 网关不再推 → 该搬到云端

    move = asyncio.create_task(client._sync_props_sources_async())
    await in_flight.wait()
    unsub = asyncio.create_task(client.unsub_device_props_async("d1"))
    await asyncio.sleep(0)  # 让它跑到取锁那一步
    await asyncio.sleep(0)
    assert not unsub.done(), "业务退订的镜像改动插进了在飞搬运 —— 镜像会被写谎"

    gate.set()
    await asyncio.gather(move, unsub)

    assert client._props_sub_dids == set()
    assert client._props_sub_sources == {}
    # 帐面清干净了，网关侧也不该留下任何一条注册
    assert hub.props_sub_groups("d1") == set()


async def test_business_subscriptions_do_not_serialize_behind_each_other():
    """业务订/退的**网络阶段**不持锁：那批是并发对账，串起来就是塌方。

    上层 `_reconcile_subscriptions` 按 `_RECONCILE_CONCURRENCY` 并发对账，并把
    「锁只保护意图集的读改写、不跨网络 I/O」立成了明文原则 —— 一次云端 SUBACK
    最长等 10s，跨 I/O 持锁会把整批串成一条，首次登录全量订属性时墙钟从
    `ceil(N/16)×RTT` 变成 `N×RTT`。这条用例把 d1 的云端订阅按在网关上，
    d2 必须在它落地前就能订完。
    """
    log: list = []
    client, log = _client(_FakeHub(log, pushable=set()))  # 都走云端
    inner = client._mips_cloud.sub_device_props_async
    started = asyncio.Event()
    gate = asyncio.Event()

    async def slow_first(did, handler):
        if did == "d1":
            started.set()
            await gate.wait()
        await inner(did, handler)

    client._mips_cloud.sub_device_props_async = AsyncMock(side_effect=slow_first)

    first = asyncio.create_task(client.sub_device_props_async("d1"))
    await started.wait()
    try:
        # d1 还在网关上挂着，d2 不该等它
        await asyncio.wait_for(client.sub_device_props_async("d2"), timeout=1.0)
    finally:
        gate.set()
        await first

    assert ("cloud+", "d2") in log
    assert client._props_sub_sources == {"d1": SOURCE_CLOUD, "d2": SOURCE_CLOUD}


async def test_a_did_dropped_mid_flight_leaves_no_stray_subscription():
    """订阅返回前这台被移出意图集 → 刚订上的那条要退掉。

    能这么插进来的是 mips 重放那条路：它的丢弃循环**不取** `_props_source_lock`，
    直接改集合与镜像。漏了这一步，网关上会留下一条镜像里没有、后续对账也扫不到的
    订阅 —— 推送一直投到下游再被家庭闸门丢掉：不报错，但白烧一条订阅。
    """
    log: list = []
    hub = _FakeHub(log, pushable=set())  # 先在云端
    client, log = _client(hub)
    await client.sub_device_props_async("d1")
    assert client._props_sub_sources["d1"] == SOURCE_CLOUD

    inner = hub.sub_device_props_async

    async def drop_then_subscribe(did, handler):
        client._props_sub_dids.discard(did)
        client._props_sub_sources.pop(did, None)
        await inner(did, handler)

    hub.sub_device_props_async = drop_then_subscribe
    hub._pushable = {"d1"}  # 网关能推了 → 该搬到本地

    await client._sync_props_sources_async()

    assert log == [("cloud+", "d1"), ("local+", "d1"), ("local-", "d1", "gA")]
    assert client._props_sub_dids == set()
    assert client._props_sub_sources == {}
    assert hub.props_sub_groups("d1") == set()


async def test_a_device_rehomed_between_gateways_reissues_on_the_new_one():
    """设备在两台都活着的网关之间迁移：通道没变，但注册还挂在旧实例上。

    设备表已指向新网关 —— `old == want` 判不出变化、差集为空，推送静默停掉且没有
    自愈入口（旧实例一直连着，transport reset 不会发生）。而退旧必须**定向**到旧
    实例：不给定目标就会把刚订上的新注册一起摘掉。
    """
    hub = _FakeHub([], pushable={"d1"}, groups={"d1": "gA"})
    client, log = _client(hub)
    await client.sub_device_props_async("d1")
    assert hub.props_sub_groups("d1") == {"gA"}

    hub.groups["d1"] = "gB"  # 设备改嫁：从 A 的清单消失、出现在 B 的清单里
    await client._sync_props_sources_async()

    assert log == [("local+", "d1"), ("local+", "d1"), ("local-", "d1", "gA")]
    assert hub.props_sub_groups("d1") == {"gB"}, "注册要落在新实例上"
    assert client._props_sub_sources["d1"] == SOURCE_LOCAL_CENTRAL


async def test_a_concurrent_subscribe_on_the_other_channel_is_taken_back():
    """两次并发订阅的**来源快照不同**时，后落地的那条要退掉自己。

    先落地的那次把镜像记成它订的通道；我们订的另一条通道上的注册镜像里没有、
    后续对账也扫不到（搬运只在 `_props_sub_dids` 上扫、且按镜像判据）—— 单源不变量
    就靠「任一时刻只有一条」，留一条就是两条都在投。同通道时反而不能退：同一 topic
    的注册是幂等的，退一次会把对方的一起摘掉。
    """
    log: list = []
    client, log = _client(_FakeHub(log, pushable=set()))  # 起始：云端
    in_flight = asyncio.Event()
    gate = asyncio.Event()

    async def slow_cloud_sub(did, handler):
        log.append(("cloud+", did))  # 请求已经出去了，但回包还没回来
        in_flight.set()
        await gate.wait()

    client._mips_cloud.sub_device_props_async = AsyncMock(side_effect=slow_cloud_sub)

    first = asyncio.create_task(client.sub_device_props_async("d1"))  # 快照 = 云端
    await in_flight.wait()
    client._central_hub._pushable = {"d1"}  # 快照期间网关能推了
    await client.sub_device_props_async("d1")  # 快照 = 本地，先落地
    assert client._props_sub_sources["d1"] == SOURCE_LOCAL_CENTRAL

    gate.set()
    await first

    # 云端那条要退掉，否则两条通道同时在投
    assert log == [("cloud+", "d1"), ("local+", "d1"), ("cloud-", "d1")]
    assert client._props_sub_sources["d1"] == SOURCE_LOCAL_CENTRAL
    assert client._central_hub.props_sub_groups("d1") == {"gA"}


async def test_a_steady_local_source_is_not_reissued():
    """注册落在设备表指的那台上 → 差集为空，不许无谓重订（切源是真实网关往返）。"""
    hub = _FakeHub([], pushable={"d1"}, groups={"d1": "gA"})
    client, log = _client(hub)
    await client.sub_device_props_async("d1")

    log.clear()
    await client._sync_props_sources_async()

    assert log == []


async def test_a_vanished_registration_is_reissued_without_unsubscribing():
    """镜像说本地、设备表也指着网关，但注册一条都没有 → 重订，且不去退不存在的东西。"""
    hub = _FakeHub([], pushable={"d1"}, groups={"d1": "gA"})
    client, log = _client(hub)
    await client.sub_device_props_async("d1")

    log.clear()
    hub.regs.clear()  # 注册没了（实例被清 / 退订竞态），镜像和 can_push 都没变

    await client._sync_props_sources_async()

    assert log == [("local+", "d1")], "没有注册可退，只订"
    assert hub.props_sub_groups("d1") == {"gA"}


async def test_a_failing_business_callback_does_not_break_the_reroute():
    """业务层回调抛异常不该把已经搬好的来源回滚掉。"""
    hub = _FakeHub([], pushable={"d1"})
    client, log = _client(hub)
    await client.sub_device_props_async("d1")

    async def boom(added, removed):
        raise RuntimeError("refresh failed")

    client._callback_dev_list_changed = boom
    hub._pushable = set()

    await client._on_dev_list_changed_from_hub([], ["d1"])

    assert client._props_sub_sources["d1"] == SOURCE_CLOUD


async def test_the_business_layer_is_not_notified_when_only_flags_changed():
    """只有能力标志翻转（added/removed 都空）时不通知业务层。

    业务层的处理是重拉设备清单 —— 一趟云端往返。标志翻转不改清单成员，
    重拉是白花的；能力标志本身由业务层自己的 online/meta 订阅更新。
    但**来源搬运照做**，那是这次回调存在的理由。
    """
    hub = _FakeHub([], pushable=set())
    client, log = _client(hub)
    await client.sub_device_props_async("d1")

    seen: list = []

    async def on_change(added, removed):
        seen.append((added, removed))

    client._callback_dev_list_changed = on_change
    hub._pushable = {"d1"}  # 只有 push_available 翻转

    await client._on_dev_list_changed_from_hub([], [])

    assert seen == [], "标志翻转不该触发业务层重拉"
    assert log == [("cloud+", "d1"), ("local+", "d1"), ("cloud-", "d1")]
    assert client._props_sub_sources["d1"] == SOURCE_LOCAL_CENTRAL


async def test_a_hub_rebuild_forces_the_local_subscription_to_be_reissued():
    """hub 被拆掉重建后，本地订阅要**重新注册**，不能只信镜像。

    本地那条订阅挂在 MipsLocalClient 实例上，teardown 会 deinit 它 —— 网关上
    注册的转发和实例一起没了。而来源镜像还记着「d1 订在本地」、`can_push` 也还是
    真，差集为空 → 永远重订不上。这些设备的属性推送就此静默死掉。

    能走到这条路的是「换号失败但旧账号 token 仍有效」：作者那侧 teardown 之后
    用 setup_central_hub_async() 重建，did 集合没变，业务层按 did 的对账也搬不动。
    """
    hub = _FakeHub([], pushable={"d1"})
    client, log = _client(hub)
    await client.sub_device_props_async("d1")  # 订在本地

    # teardown：hub 换成新的，旧实例的订阅随它一起消失
    client._central_hub = None
    client._invalidate_local_props_sources()
    assert client._props_sub_sources == {}
    new_hub = _FakeHub(log, pushable={"d1"})
    client._central_hub = new_hub

    await client._sync_props_sources_async()

    assert ("local+", "d1") in log[1:], "重建后没有重新注册本地订阅"
    # 不该顺手去退一条根本不存在的云端订阅（None 哨兵就是为这个）
    assert not any(entry[0] == "cloud-" for entry in log)
    assert client._props_sub_sources["d1"] == SOURCE_LOCAL_CENTRAL


async def test_unsubscribing_after_a_hub_rebuild_touches_no_channel():
    """重建后业务层退订这台设备：没有可退的东西，也不该去打云端。"""
    hub = _FakeHub([], pushable={"d1"})
    client, log = _client(hub)
    await client.sub_device_props_async("d1")

    client._central_hub = None
    client._invalidate_local_props_sources()

    await client.unsub_device_props_async("d1")

    assert log == [("local+", "d1")]
    assert client._props_sub_dids == set()
    assert client._props_sub_sources == {}


async def test_all_subscribes_happen_before_any_unsubscribe():
    """换源的阶段边界是全局的：一批里所有「订新」都完成，才开始「退旧」。

    逐个 await 的写法只在**单台**内部保证这个次序。一次网关断开会让整批设备
    同时换源，那时任一台的「退旧」插到另一台的「订新」前面，就出现了一段
    「这台两条都没有」的空窗 —— 空窗里落地的变更静默丢掉。
    """
    hub = _FakeHub([], pushable={"d1", "d2", "d3"})
    client, log = _client(hub)
    for did in ("d1", "d2", "d3"):
        await client.sub_device_props_async(did)
    log.clear()

    hub._pushable = set()  # 网关不推了 → 三台一起搬到云端
    await client._sync_props_sources_async()

    kinds = [entry[0] for entry in log]
    assert kinds.count("cloud+") == 3
    assert kinds.count("local-") == 3
    assert max(i for i, k in enumerate(kinds) if k == "cloud+") < min(
        i for i, k in enumerate(kinds) if k == "local-"
    ), f"阶段边界被打破: {log}"


async def test_a_local_transport_reset_reissues_local_subscriptions():
    """中枢换了新实例 → 本地订阅要重新注册。

    新实例的订阅表是空的，而设备表可能一字未变（掉线期间清单没动、重连 sweep
    直接换实例）——按 did 的对账和「设备表变化」那条路都通知不到，只有这个信号知道。
    """
    hub = _FakeHub([], pushable={"d1"})
    client, log = _client(hub)
    await client.sub_device_props_async("d1")

    await client._on_local_transport_reset()

    assert log == [("local+", "d1"), ("local+", "d1")]
    assert client._props_sub_dids == {"d1"}
    assert client._props_sub_sources["d1"] == SOURCE_LOCAL_CENTRAL


async def test_a_local_transport_reset_leaves_cloud_dids_alone():
    """重置信号只该管本地那批：云端订阅没挂在中枢实例上。"""
    hub = _FakeHub([], pushable=set())
    client, log = _client(hub)
    await client.sub_device_props_async("d1")

    await client._on_local_transport_reset()

    assert log == [("cloud+", "d1")]
    assert client._props_sub_sources["d1"] == SOURCE_CLOUD


async def test_a_did_with_no_source_is_reestablished_on_cloud():
    """teardown 把本地条目作废成 `None` 哨兵后，搬运要把这台重新订上。

    此刻网关不可用（can_push 假）就订回云端 —— 这正是「换号失败但旧账号仍有效」
    那条恢复路径要的结果。不该顺手去退一条根本不存在的云端订阅。
    """
    hub = _FakeHub([], pushable=set())
    client, log = _client(hub)
    client._props_sub_dids = {"d1"}
    client._props_sub_sources = {}  # None 哨兵：有意图、一条源都没有

    await client._sync_props_sources_async()

    assert log == [("cloud+", "d1")]
    assert client._props_sub_sources["d1"] == SOURCE_CLOUD


async def test_rebuilding_the_hub_triggers_a_source_sync(monkeypatch):
    """重建中枢后要补一次搬运 —— 那条恢复路径只重建 hub、不跑 mips 重放。

    若此后没有任何网关连上（mac 上 LNP 还没授权、网关离线、机器不在网关网段），
    那批 did 会一直停在「一条源都没有」：控制照常走云端、日志里什么都没有，
    只有重启后端能恢复 —— 而换号前它们是收得到推送的。
    """
    client = object.__new__(MIoTClient)
    client._central_hub_enabled = True
    client._central_hub = None
    client._storage = object()
    client._http_client = object()
    client._cloud_server = "cn"
    client._oauth_info = SimpleNamespace(user_info=SimpleNamespace(uid="u1"))
    client._main_loop = asyncio.get_running_loop()
    client._props_sub_dids = {"d1"}
    client._props_sub_sources = {}  # None 哨兵
    client._props_source_lock = asyncio.Lock()
    log: list = []
    client._mips_cloud = SimpleNamespace(
        is_connected=True,
        sub_device_props_async=AsyncMock(
            side_effect=lambda did, handler: log.append(("cloud+", did))
        ),
        unsub_device_props_async=AsyncMock(),
    )

    class _Hub:
        enabled = True
        on_dev_list_changed = None
        on_local_transport_reset = None

        def can_push(self, did):
            return False  # 网关还没连上

        async def init_async(self):
            pass

    monkeypatch.setattr(client_mod, "CentralHubManager", lambda **_kw: _Hub())

    await client.setup_central_hub_async()

    assert log == [("cloud+", "d1")]
    assert client._props_sub_sources["d1"] == SOURCE_CLOUD
