"""cycle 级聚合:N 个 DeviceTraceRecord → 1 个 CycleTraceRecord。

算子:SUM / OR / AND / COUNT。

注意 omni_call_count / omni_error_count 是 cycle 级二值(0/1):
batch 推理整体调一次 omni,任一 device 抛 OmniError 都让整 batch reraise,
失败时 processor 给所有 device 都打同一 error_code。所以这两项必须按
cycle 算,否则 N 镜头部署下错误数会被虚高 N 倍。
"""
from __future__ import annotations

from typing import Any

from miloco.observability.types import (
    CycleTraceRecord,
    DeviceTraceRecord,
)


def aggregate_cycle(
    device_records: list[DeviceTraceRecord],
    cycle_meta: dict[str, Any],
) -> CycleTraceRecord:
    gates = [d.gate for d in device_records]
    identities = [d.identity for d in device_records if d.identity is not None]
    omnis = [d.omni for d in device_records if d.omni is not None]
    requests = [o for o in omnis if o.error_code != "CircuitOpenError"]

    skipped = (not gates) or all(g.skipped for g in gates)

    return CycleTraceRecord(
        device_count=len(device_records),
        skipped=skipped,
        gate_ms=sum(g.ms for g in gates),
        gate_video_ms=sum(g.video_ms for g in gates),
        gate_audio_ms=sum(g.audio_ms for g in gates),
        gate_video_pass=any(g.video_pass for g in gates),
        gate_audio_pass=any(g.audio_pass for g in gates),
        gate_hold_pass=any(g.hold_pass for g in gates),
        identity_ms=sum(i.ms for i in identities),
        omni_ms=sum(o.ms for o in omnis),
        omni_wall_ms=max((o.ms for o in omnis), default=0.0),
        omni_request_count=len(requests),
        omni_request_error_count=sum(o.error_code is not None for o in requests),
        omni_call_count=1 if omnis else 0,
        omni_error_count=1 if any(o.error_code is not None for o in omnis) else 0,
        dropped_windows_total=sum(d.dropped_windows_count for d in device_records),
        overflow_count_total=sum(d.overflow_count for d in device_records),
        partial_windows_total=sum(d.partial_windows_count for d in device_records),
        **cycle_meta,
    )
