# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""action 返回体的正典形状 —— 归一规则 + 云端通道映射。

fixture 全部是 **2026-10-12 在 cat@mac 上实测抓到的真实回包**（同一台人在传感器
Pro 的 ``get-tracking-info`` 走两条通道），不是照文档构造的：

- 本地中枢（MIPS ``proxy/rpcReq`` 的 payload 顶层）
- 云端（MiHome ``/app/v2/miotspec/action`` 的 ``result``）

无真实网络/凭据/设备依赖 —— CI 的 `miot 单元测试` 步骤按 `-m unit` 选择。
"""

import pytest
from miot.action_result import (
    SOURCE_CLOUD,
    build_action_result,
    fill_missing_out_piids,
    normalize_out,
)
from miot.cloud import MIoTHttpClient
from miot.types import MIoTActionParam

pytestmark = pytest.mark.unit

_DID = "2032249710"

# ─── 实测 fixture ─────────────────────────────────────────────────────────────

# 人在传感器Pro get-tracking-info 的追踪 JSON（内容本身不重要，形状才重要）
_TRACKING_JSON = '{"l":182,"p":[[2831,325,163,489,21202159]],"mv":0,"mn":1,"ota":0,"rp":1}'

# 本地中枢：网关回包 payload（exe_time 在**顶层**，与 result 平级）
GATEWAY_REPLY_LOCAL = {
    "id": 1778597798,
    "result": {"code": 0, "out": [{"piid": 7, "value": _TRACKING_JSON}]},
    "exe_time": 50,
}

# 云端成功：out 被拍平成裸标量，piid 没了；另带一串本地没有的遥测
CLOUD_RESULT_OK = {
    "did": _DID,
    "miid": 0,
    "siid": 3,
    "aiid": 4,
    "code": 0,
    "out": [_TRACKING_JSON],
    "exe_time": 19,
    "net_cost": 213,
    "ot_cost": 27,
    "otlocalts": 1791811604428651,
    "_oa_rpc_cost": 402,
    "withLatency": 0,
}

# 云端失败：**连 out 键都没有**（get-zone-list 少传参数，实测 -704220025）
CLOUD_RESULT_FAIL = {
    "did": _DID,
    "miid": 0,
    "siid": 3,
    "aiid": 2,
    "code": -704220025,
    "exe_time": 0,
    "withLatency": 0,
}

_TELEMETRY_DROPPED = ("ot_cost", "otlocalts", "_oa_rpc_cost", "withLatency")


# ─── normalize_out ───────────────────────────────────────────────────────────


def test_normalize_out_local_form_keeps_piid():
    """本地形态 ``{"piid", "value"}`` 原样保留 —— piid 是权威值。"""
    assert normalize_out([{"piid": 7, "value": "x"}]) == [{"piid": 7, "value": "x"}]


def test_normalize_out_cloud_form_fills_piid_none():
    """云端形态是裸标量（piid 被拍平）→ 归一这一步先填 None。

    这是**中间态**：``MIoTClient`` 随后会用 :func:`fill_missing_out_piids` 按 spec
    声明把 piid 补回来（见本文件后半与 ``test_client_control_routing.py``）。
    这里只钉"归一本身不猜"——归一层拿不到 spec，猜了就会得到一个"看起来完全正常、
    只在多输出动作上错位"的 piid。
    """
    assert normalize_out(["x"]) == [{"piid": None, "value": "x"}]


def test_normalize_out_missing_key_becomes_empty():
    """云端失败时根本没有 out 键 → 空列表，而不是抛异常。"""
    assert normalize_out(None) == []


def test_normalize_out_non_list_is_empty():
    assert normalize_out({"piid": 7}) == []
    assert normalize_out("x") == []


def test_normalize_out_non_int_piid_becomes_none():
    assert normalize_out([{"piid": "7", "value": "x"}]) == [{"piid": None, "value": "x"}]


def test_normalize_out_is_shape_driven_not_source_driven():
    """两种形态混在同一条 out 里也各自判对 —— 归一不依赖 source。"""
    assert normalize_out([{"piid": 7, "value": "a"}, "b"]) == [
        {"piid": 7, "value": "a"},
        {"piid": None, "value": "b"},
    ]


# ─── fill_missing_out_piids ──────────────────────────────────────────────────


def test_fill_piids_by_position():
    out = [{"piid": None, "value": "a"}]
    assert fill_missing_out_piids(out, [7]) is True
    assert out == [{"piid": 7, "value": "a"}]


def test_fill_does_not_overwrite_authoritative_piid():
    """本地中枢那条的 piid 是网关给的权威值 —— 一律不覆盖。"""
    out = [{"piid": 7, "value": "a"}]
    assert fill_missing_out_piids(out, [9]) is False
    assert out == [{"piid": 7, "value": "a"}]


def test_fill_refuses_on_count_mismatch():
    """数量不符 = spec 与上游对这条 action 的理解不一致 → 不赌顺序，整条留空。"""
    out = [{"piid": None, "value": "a"}, {"piid": None, "value": "b"}]
    assert fill_missing_out_piids(out, [7]) is False
    assert [it["piid"] for it in out] == [None, None]


def test_fill_refuses_without_declared_piids():
    """spec 里查不到这条 action（或它没有输出）→ 不补。"""
    out = [{"piid": None, "value": "a"}]
    assert fill_missing_out_piids(out, []) is False
    assert out == [{"piid": None, "value": "a"}]


# ─── build_action_result ─────────────────────────────────────────────────────


def test_net_cost_is_latency_minus_exe_time():
    r = build_action_result(
        did="d", siid=3, aiid=4, code=0, source=SOURCE_CLOUD, latency=213, exe_time=19
    )
    assert r["net_cost"] == 194


def test_net_cost_none_when_exe_time_unknown():
    """exe_time 未知时 net_cost 只能是 None —— 编一个数出来就是假对等。"""
    r = build_action_result(
        did="d", siid=3, aiid=4, code=0, source=SOURCE_CLOUD, latency=213
    )
    assert r["exe_time"] is None and r["net_cost"] is None


def test_net_cost_none_when_never_sent():
    """从没真正发出过请求的分支（本地返回 None 那条）量不到往返。"""
    r = build_action_result(did="d", siid=3, aiid=4, code=-10041, source="local_central")
    assert r["latency"] is None and r["net_cost"] is None


def test_bool_is_not_accepted_as_int():
    """bool 是 int 子类，别把它当毫秒数算进 net_cost。"""
    r = build_action_result(
        did="d", siid=3, aiid=4, code=0, source=SOURCE_CLOUD, latency=100, exe_time=True
    )
    assert r["exe_time"] is None and r["net_cost"] is None


def test_canonical_shape_has_every_contract_key():
    r = build_action_result(
        did="d", siid=3, aiid=4, code=0, source=SOURCE_CLOUD, out_raw=["x"], latency=1, exe_time=1
    )
    assert set(r) == {
        "did", "siid", "aiid", "code", "source", "out", "exe_time", "latency", "net_cost",
    }


# ─── 云端通道：实测回包走一遍 action_async ────────────────────────────────────


def _stub_cloud_post(monkeypatch, result):
    """把私有 POST 换掉，只喂 result，不碰网络/加解密。"""

    async def fake_post(self, url_path, data, timeout=None):
        return {"result": result}

    monkeypatch.setattr(
        MIoTHttpClient, "_MIoTHttpClient__mihome_api_post_async", fake_post
    )


@pytest.mark.asyncio
async def test_cloud_action_flattens_out_and_keeps_exe_time(monkeypatch):
    _stub_cloud_post(monkeypatch, CLOUD_RESULT_OK)
    http = MIoTHttpClient(cloud_server="cn", access_token="tok")
    res = await http.action_async(
        MIoTActionParam(did=_DID, siid=3, aiid=4, in_=[])
    )

    assert res["code"] == 0
    assert res["source"] == SOURCE_CLOUD
    assert res["did"] == _DID and res["siid"] == 3 and res["aiid"] == 4
    # 云端那个扁平数组被包成 piid=None，piid 不猜
    assert res["out"] == [{"piid": None, "value": _TRACKING_JSON}]
    assert res["exe_time"] == 19
    assert isinstance(res["latency"], int) and res["latency"] >= 0
    assert res["net_cost"] == res["latency"] - 19


@pytest.mark.asyncio
async def test_cloud_action_drops_the_rest_of_the_telemetry(monkeypatch):
    """上游那串遥测只留 exe_time。

    ``net_cost`` 本身是契约字段,但**值**必须来自 SDK 自算、不是上游那个 213 ——
    上游的只覆盖"云端→设备"一段,和本地中枢的局域网往返不是同一条路径。
    """
    _stub_cloud_post(monkeypatch, CLOUD_RESULT_OK)
    http = MIoTHttpClient(cloud_server="cn", access_token="tok")
    res = await http.action_async(MIoTActionParam(did=_DID, siid=3, aiid=4, in_=[]))

    for key in _TELEMETRY_DROPPED:
        assert key not in res
    # 定义式的断言(stub 里没有真实往返,所以不去断言它的具体数值)
    assert res["net_cost"] == res["latency"] - res["exe_time"]
    assert res["net_cost"] != CLOUD_RESULT_OK["net_cost"]


@pytest.mark.asyncio
async def test_cloud_action_failure_has_empty_out(monkeypatch):
    """实测：云端失败时连 out 键都没有 → 归一出空列表，code 原样保留。"""
    _stub_cloud_post(monkeypatch, CLOUD_RESULT_FAIL)
    http = MIoTHttpClient(cloud_server="cn", access_token="tok")
    res = await http.action_async(MIoTActionParam(did=_DID, siid=3, aiid=2, in_=[]))

    assert res["code"] == -704220025
    assert res["out"] == []
    assert res["exe_time"] == 0
    assert res["source"] == SOURCE_CLOUD


@pytest.mark.asyncio
async def test_cloud_action_non_dict_result_is_still_canonical(monkeypatch):
    """旧代码把非 dict 的 result 原样上抛，上层只在 dict 上取 code，等于静默无结论。"""
    _stub_cloud_post(monkeypatch, 5)
    http = MIoTHttpClient(cloud_server="cn", access_token="tok")
    res = await http.action_async(MIoTActionParam(did=_DID, siid=3, aiid=4, in_=[]))

    assert res["code"] is None and res["out"] == []
