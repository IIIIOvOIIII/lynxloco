# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Action 返回体的**正典形状** —— 云端与本地中枢两条通道都必须收敛到这里。

为什么需要
----------

同一个 action 走不同通道时，上游给的信封**整体**不同。下面是同一台人在传感器
Pro 的 `action.3.4`（get-tracking-info）的实测回包：

- 本地中枢（MIPS ``proxy/rpcReq``）：::

      {"code": 0, "out": [{"piid": 7, "value": "<json>"}]}

- 云端（MiHome ``/app/v2/miotspec/action``）：::

      {"did": "…", "miid": 0, "siid": 3, "aiid": 4, "code": 0,
       "out": ["<json>"], "exe_time": 19, "net_cost": 213, …}

差异不止 ``out``：云端带一串本地没有的遥测字段，**失败时连 `out` 键都没有**，
而 `out` 本身一边是 ``{"piid", "value"}`` 一边是裸标量。

调用方（规则引擎 / MCP / agent）拿到哪种形状取决于**路由决策**，而路由会因为
超时冷却、网关掉线在同一个设备上来回翻转 —— 所以这不是"初始化时分支一次"的
差异，而是一个会当着调用者面切换的形状。归一只需一次，放在两条通道各自的出口。

约定
----

- **解析不许看 `source`。** ``out`` 一律由 :func:`normalize_out` 按元素形态判定，
  ``source`` 只用于让调用方判断**能力差异**（例如某些字段只有云端提供），
  不用来判断形状。否则就把正确性押在"source 与形状一一对应"这个会失效的假设上。
- **云端被拍平的 piid 要补回来，不能只留 ``None``。** 只统一形状而留空 piid，
  下游照样得为"这条走了哪条通道"写两套解析 —— 那这个兼容就等于没做。补法是
  按位对照设备 spec 里该 action 声明的 ``out`` 列表（见
  :func:`fill_missing_out_piids`），**只在数量对得上时补**：数量不符说明 spec 与
  上游对同一条 action 的理解不一致，此时宁可留 ``None`` 也不猜位置。
- **`net_cost` 由 SDK 自算，不取上游的。** 上游的 ``net_cost`` 只覆盖"云端→设备"
  一段，而本地那次往返走的是局域网，两者不是同一条路径、直接比会得到假对等。
  两条通道都用 SDK 自己量的 ``latency`` 减 ``exe_time``，口径才一致。
- ``exe_time`` 本地也**有**：网关把它放在回包顶层、``result`` 的兄弟位置，
  早先被 ``result_obj.get("result")`` 连同一起丢掉了。
"""

from __future__ import annotations

from typing import Any, Optional, TypedDict

__all__ = [
    "SOURCE_CLOUD",
    "SOURCE_LOCAL_CENTRAL",
    "MIoTActionOutItem",
    "MIoTActionResult",
    "normalize_out",
    "fill_missing_out_piids",
    "build_action_result",
]

SOURCE_CLOUD = "cloud"
SOURCE_LOCAL_CENTRAL = "local_central"


class MIoTActionOutItem(TypedDict):
    """``out`` 的一项。

    ``piid`` 为 ``None`` 表示**没能拿到**（云端把 piid 拍平了，且 spec 不可用或
    spec 声明的输出个数与云端返回的对不上）。正常情况下云端那条也会由
    :func:`fill_missing_out_piids` 从 spec 按位补回来 —— 只有补不了才留空。
    """

    piid: Optional[int]
    value: Any


class MIoTActionResult(TypedDict):
    """``action_async`` 的正典返回形状（两条通道同形）。

    - ``code``：0 成功；非 0 为设备级拒绝或通道错误码。**路由层依赖它做双发
      判定**（超时 / 结果未知 / 可安全重发），必须保持在顶层。
    - ``source``：这条结果实际走了哪条通道，取值见 ``SOURCE_*``。
    - ``out``：见 :func:`normalize_out`。
    - ``latency``：SDK 自己量的端到端往返毫秒数（发出 → 收到）；未真正发出
      请求的分支为 ``None``。
    - ``exe_time``：设备执行耗时毫秒数，取自上游；上游没给则为 ``None``。
    - ``net_cost``：``latency - exe_time``；两者有一个未知则为 ``None``。
    """

    did: str
    siid: int
    aiid: int
    code: Optional[int]
    source: str
    out: list[MIoTActionOutItem]
    exe_time: Optional[int]
    latency: Optional[int]
    net_cost: Optional[int]


def normalize_out(raw: Any) -> list[MIoTActionOutItem]:
    """把上游两种 ``out`` 形态归一到 ``[{"piid", "value"}]``。

    - ``{"piid": …, "value": …}``（本地中枢形态）→ 原样，piid 非 int 时置 None。
    - 裸标量（云端形态，piid 被拍平）→ ``{"piid": None, "value": <标量>}``。
    - 非 list / None（云端**失败时根本没有 `out` 键**）→ ``[]``。

    形态按**元素**判定而不是按 ``source`` 判定：上游哪天改了形状，这里退化成
    留空而不是静默错位。
    """
    if not isinstance(raw, list):
        return []
    out: list[MIoTActionOutItem] = []
    for item in raw:
        if isinstance(item, dict) and "value" in item:
            piid = item.get("piid")
            out.append(
                {"piid": piid if isinstance(piid, int) else None, "value": item["value"]}
            )
        else:
            out.append({"piid": None, "value": item})
    return out


def fill_missing_out_piids(
    out: list[MIoTActionOutItem], declared_piids: list[int]
) -> bool:
    """按位把 ``out`` 里缺失的 ``piid`` 补上（原地改），返回是否补过。

    ``declared_piids`` 是设备 spec 里该 action 声明的 ``out`` 列表，**按声明顺序**。
    云端那条被拍平的 ``out`` 就靠这个还原成和本地中枢同形。

    **只在数量对得上时补**：数量不符说明 spec 与上游对同一条 action 的理解不一致，
    这时按位对齐就是在赌顺序，宁可整条留空。已经带了 ``piid`` 的项（本地中枢那条
    的权威值）一律不覆盖。
    """
    if not declared_piids or len(declared_piids) != len(out):
        return False
    filled = False
    for item, piid in zip(out, declared_piids):
        if item.get("piid") is None:
            item["piid"] = piid
            filled = True
    return filled


def build_action_result(
    *,
    did: str,
    siid: int,
    aiid: int,
    code: Optional[int],
    source: str,
    out_raw: Any = None,
    exe_time: Any = None,
    latency: Any = None,
) -> MIoTActionResult:
    """拼出正典形状；``net_cost`` 在这里算，调用方不要自己再减一遍。"""
    exe_time_ms = exe_time if isinstance(exe_time, int) and not isinstance(exe_time, bool) else None
    latency_ms = latency if isinstance(latency, int) and not isinstance(latency, bool) else None
    net_cost = (
        latency_ms - exe_time_ms
        if latency_ms is not None and exe_time_ms is not None
        else None
    )
    return {
        "did": did,
        "siid": siid,
        "aiid": aiid,
        "code": code,
        "source": source,
        "out": normalize_out(out_raw),
        "exe_time": exe_time_ms,
        "latency": latency_ms,
        "net_cost": net_cost,
    }
