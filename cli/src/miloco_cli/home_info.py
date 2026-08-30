"""home_info 数据获取（无本地缓存，每次从后端拉取）。

- 控制校验：did 存在、iid 在 spec 中、value 在 range 内
"""

import re

from miloco_cli.catalog import normalize_desc

# ─── 数据获取 ──────────────────────────────────────────────────────────────────


def _fetch(*, refresh: bool = False, timeout: float | None = None) -> dict:
    """从后端拉取 home_info 并返回。refresh=True 触发后端刷新云端数据。"""
    from miloco_cli.client import api_get

    path = "/api/devices/home"
    if refresh:
        path += "?refresh=true"
    resp = api_get(path, timeout=timeout)
    return resp.get("data", {})


def get_home_info(*, refresh: bool = False, timeout: float | None = None) -> dict:
    """返回 home_info（每次从后端拉取）。refresh=True 触发后端刷新云端数据。"""
    return _fetch(refresh=refresh, timeout=timeout)


# ─── 查询助手 ─────────────────────────────────────────────────────────────────


def list_devices(
    room: str | None = None,
    category: str | None = None,
    online_only: bool = False,
) -> list[dict]:
    info = get_home_info()
    devices = info.get("devices", [])
    if room:
        devices = [d for d in devices if d.get("room") == room]
    if category:
        devices = [d for d in devices if d.get("category") == category]
    if online_only:
        devices = [d for d in devices if d.get("online")]
    return devices


def list_scenes() -> list[dict]:
    return get_home_info().get("scenes", [])


def list_persons() -> list[dict]:
    return get_home_info().get("persons", [])


# ─── 参数校验 ─────────────────────────────────────────────────────────────────


class ValidationError(Exception):
    pass


def validate_did(did: str, info: dict | None = None) -> dict:
    """校验 did 存在，返回设备 dict。"""
    if info is None:
        info = get_home_info()
    for dev in info.get("devices", []):
        if dev.get("did") == did:
            return dev
    raise ValidationError(
        f"did '{did}' not found. Run `device list` to see available dids."
    )


def validate_iid(did: str, iid: str, info: dict | None = None) -> dict:
    """校验 iid 在设备 spec 中，返回 spec 条目。spec 为空时跳过校验返回空 dict。"""
    dev = validate_did(did, info)
    spec = dev.get("spec", {})
    if not spec or not isinstance(spec, dict):
        # spec 未填充时跳过离线校验，由服务端负责参数验证
        return {}
    if iid not in spec:
        raise ValidationError(f"iid '{iid}' not in spec of device '{did}'")
    return spec[iid]


def validate_value(iid_spec: dict, value) -> None:
    """本地校验 value：枚举（value_list）优先，其次数值范围（value_range）。

    枚举不合法时列出全部合法取值；范围越界时带上 step 与单位，方便 agent 改对。
    """
    # 枚举校验：spec 给了 value_list 就必须命中其中之一
    value_list = iid_spec.get("value_list")
    if isinstance(value_list, list) and value_list:
        allowed = [it for it in value_list if isinstance(it, dict)]
        if value not in {it.get("value") for it in allowed}:
            opts = ", ".join(f"{it.get('name')}={it.get('value')}" for it in allowed)
            raise ValidationError(
                f"value {value!r} is not a valid enum; allowed: {opts}"
            )
        return

    # 数值范围校验
    value_range = iid_spec.get("value_range")
    if not value_range or len(value_range) < 2 or not isinstance(value, (int, float)):
        return
    lo, hi = value_range[0], value_range[1]
    if not (lo <= value <= hi):
        step = value_range[2] if len(value_range) >= 3 else None
        rng = f"[{lo},{hi}" + (f";{step}" if step is not None else "") + "]"
        unit = iid_spec.get("unit")
        suffix = f" {unit}" if unit else ""
        raise ValidationError(f"value {value} out of range {rng}{suffix}")


def infer_value(raw: str):
    """自动推断 value 类型：true/false → bool, 数字 → int/float, 其余 → str。"""
    if raw.lower() == "true":
        return True
    if raw.lower() == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


# ─── 控制命令的 type_name → iid 反查 ──────────────────────────────────────────


_BOOL_TRUE = {"true", "1", "on", "yes"}
_BOOL_FALSE = {"false", "0", "off", "no"}


def normalize_bool(raw):
    """bool 兼容：把 ``true/True/1/on/yes`` 归一为 ``True``，
    ``false/False/0/off/no`` 归一为 ``False``。其它原样返回。

    模型在不同语境下会输出 ``true/false`` 或 ``0/1`` 或 ``on/off``，
    CLI 兜底接受多种写法。
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and raw in (0, 1):
        return bool(raw)
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low in _BOOL_TRUE:
            return True
        if low in _BOOL_FALSE:
            return False
    return raw


_KEY_BARE_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
# desc 部分允许 Unicode（含中文）：排除空白与 TSV / 解析层冲突字符
_KEY_DESC_RE = re.compile(r"^([a-z0-9][a-z0-9.-]*)@([^\s|,:=@]+)$")
_IID_RE = re.compile(r"^(prop|action)\.(\d+)\.(\d+)$")


def _iter_spec_entries(spec):
    if not isinstance(spec, dict):
        return
    for iid, entry in spec.items():
        if isinstance(entry, dict):
            yield iid, entry


_ANNOTATION_RE = re.compile(r"\([^)]*\)\s*$")


def lookup_iid_by_key(did: str, key: str, info: dict | None = None) -> str:
    """把控制命令第二参解析成具体 iid。

    接受三种形态：
    - ``prop.{s}.{p}`` / ``action.{s}.{p}``：原样返回（向后兼容）
    - ``type_name``：唯一映射就用；多映射 → 报错并列出候选 iid
    - ``type_name@{service_description}``：按规范化后的 description 比对

    catalog / device spec 输出的 key 可能带 ``(注释)`` 后缀（如
    ``on@左键(开关)``、``prop.2.1(左键 开关)``），入口处自动剥掉。
    """
    # 剥 catalog 展示注释 (...)
    key = _ANNOTATION_RE.sub("", key)

    # 已是 iid，直接返回
    if _IID_RE.match(key):
        return key

    # 解析 type_name + 可选 service_description：两种形态共用同一套候选过滤
    m_desc = _KEY_DESC_RE.match(key)
    if m_desc:
        type_name, desc_norm = m_desc.group(1), m_desc.group(2)
    elif _KEY_BARE_RE.match(key):
        type_name, desc_norm = key, None
    else:
        raise ValidationError(
            f"unrecognized spec_name '{key}'. Expected a spec_name, "
            "spec_name@<子设备描述>, or prop.s.p / action.s.p."
        )

    if info is None:
        info = get_home_info()
    dev = validate_did(did, info)
    spec = dev.get("spec", {})

    candidates: list[tuple[str, dict]] = []
    for iid, entry in _iter_spec_entries(spec):
        if entry.get("type_name") != type_name:
            continue
        if desc_norm is not None and (
            normalize_desc(entry.get("service_description")) != desc_norm
        ):
            continue
        candidates.append((iid, entry))

    if len(candidates) == 1:
        return candidates[0][0]

    if len(candidates) > 1:
        lines = [
            f"  {iid} ({entry.get('service_description') or entry.get('description') or ''})"
            for iid, entry in candidates
        ]
        raise ValidationError(
            f"spec_name '{key}' matches {len(candidates)} iids on '{did}', "
            f"use spec_name@<子设备描述> or prop.s.p / action.s.p to disambiguate:\n"
            + "\n".join(lines)
        )

    # 0 候选
    if desc_norm is not None:
        raise ValidationError(
            f"spec_name '{key}' not matched on '{did}'. "
            "Run `device spec` to see available spec_names."
        )
    if not isinstance(spec, dict) or not spec:
        raise ValidationError(
            f"spec_name '{key}' on '{did}' cannot be resolved: spec empty, "
            "backend may still be loading."
        )
    if not any(e.get("type_name") for _, e in _iter_spec_entries(spec)):
        raise ValidationError(
            f"spec_name '{key}' on '{did}' cannot be resolved: "
            "device spec has no named entries."
        )
    raise ValidationError(
        f"spec_name '{key}' not found on '{did}'. "
        "Run `device spec` to see available spec_names."
    )
