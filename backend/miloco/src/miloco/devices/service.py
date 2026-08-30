# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Unified smart-home device service."""

from __future__ import annotations

from typing import Any

from miloco.devices.schema import (
    DeviceSource,
    UnifiedActionResult,
    UnifiedDeviceControlRequest,
    UnifiedDeviceInfo,
    UnifiedHomeInfo,
    UnifiedSceneInfo,
    UnifiedSpecEntry,
    parse_ha_device_id,
)


class DevicesService:
    """Merge MIoT and Home Assistant behind one source-aware API."""

    def __init__(self, miot_service, home_assistant_service) -> None:
        self._miot_service = miot_service
        self._ha_service = home_assistant_service

    async def home(self, *, refresh: bool = False) -> UnifiedHomeInfo:
        """Return merged smart-home inventory."""
        miot_home = await self._miot_home(refresh=refresh)
        ha_devices = await self._ha_service.list_imported_devices(refresh=refresh)
        ha_scenes = await self._ha_service.list_scenes()
        devices = [*miot_home.devices, *ha_devices]
        return UnifiedHomeInfo(
            home_name=miot_home.home_name,
            devices=devices,
            scenes=[*miot_home.scenes, *ha_scenes],
            areas=_merge_areas(miot_home.areas, devices),
        )

    async def get_spec(self, device_id: str) -> UnifiedDeviceInfo:
        """Return one source-aware device spec."""
        parsed = parse_ha_device_id(device_id)
        if parsed is not None:
            return await self._ha_service.get_device(parsed[1], refresh=False)
        return miot_device_to_unified(await self._miot_service.get_device_spec(device_id))

    async def control(
        self,
        device_id: str,
        request: UnifiedDeviceControlRequest,
    ) -> UnifiedActionResult:
        """Dispatch control by device source."""
        parsed = parse_ha_device_id(device_id)
        if parsed is not None:
            return await self._ha_service.control(parsed[1], request)
        data = await self._miot_service.control_device(
            device_id,
            request.to_miot_request(),
        )
        return UnifiedActionResult(
            success=True,
            source=DeviceSource.MIOT,
            did=device_id,
            message="MIoT control executed",
            data=data,
        )

    async def status(
        self,
        device_id: str,
        iids: list[str] | None,
    ) -> dict[str, object]:
        """Return current readable property values."""
        parsed = parse_ha_device_id(device_id)
        if parsed is not None:
            device = await self._ha_service.get_device(parsed[1], refresh=False)
            selected = set(iids or device.spec.keys())
            return {
                "properties": [
                    {"iid": iid, "value": None, "code": 0}
                    for iid in selected
                    if iid in device.spec
                ]
            }
        return await self._miot_service.get_device_status(device_id, iids)

    async def trigger_scene(self, scene_id: str) -> UnifiedActionResult:
        """Trigger a source-aware scene."""
        parsed = parse_ha_device_id(scene_id)
        if parsed is not None:
            request = UnifiedDeviceControlRequest(type="call_action", iid="activate")
            return await self._ha_service.control(parsed[1], request)
        ok = await self._miot_service.trigger_scene(scene_id)
        return UnifiedActionResult(
            success=ok,
            source=DeviceSource.MIOT,
            did=scene_id,
            message="MIoT scene triggered" if ok else "MIoT scene trigger failed",
            data=None,
        )

    async def _miot_home(self, *, refresh: bool = False) -> UnifiedHomeInfo:
        home = await self._miot_service.get_home_info(refresh=refresh)
        devices = [
            miot_device_to_unified(item)
            for item in home.get("devices", [])
            if isinstance(item, dict)
        ]
        scenes = [
            miot_scene_to_unified(item)
            for item in home.get("scenes", [])
            if isinstance(item, dict)
        ]
        return UnifiedHomeInfo(
            home_name=home.get("home_name") or "",
            devices=devices,
            scenes=scenes,
            areas=_normalize_areas(home.get("areas", [])),
        )


def miot_device_to_unified(item: dict[str, Any]) -> UnifiedDeviceInfo:
    """Convert the existing MIoT device dict into the unified device model."""
    spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
    return UnifiedDeviceInfo(
        did=str(item.get("did") or ""),
        source=DeviceSource.MIOT,
        source_label="Xiaomi",
        name=str(item.get("name") or item.get("did") or ""),
        online=bool(item.get("online")),
        model=_optional_str(item.get("model")),
        room=_optional_str(item.get("room") or item.get("room_name")),
        category=_optional_str(item.get("category")),
        spec={
            str(iid): _spec_entry_from_dict(str(iid), entry)
            for iid, entry in spec.items()
            if isinstance(entry, dict)
        },
        included=True,
        control_enabled=True,
    )


def miot_scene_to_unified(item: dict[str, Any]) -> UnifiedSceneInfo:
    """Convert the existing MIoT scene dict into the unified scene model."""
    return UnifiedSceneInfo(
        scene_id=str(item.get("scene_id") or ""),
        scene_name=str(item.get("scene_name") or item.get("name") or ""),
        source=DeviceSource.MIOT,
        source_label="Xiaomi",
        executable=True,
    )


def _spec_entry_from_dict(iid: str, entry: dict[str, Any]) -> UnifiedSpecEntry:
    return UnifiedSpecEntry(
        iid=str(entry.get("iid") or iid),
        type_name=str(entry.get("type_name") or entry.get("name") or iid),
        description=str(entry.get("description") or ""),
        service_type_name=str(entry.get("service_type_name") or ""),
        service_description=str(entry.get("service_description") or ""),
        prop_description=str(entry.get("prop_description") or ""),
        format=str(entry.get("format") or ""),
        readable=bool(entry.get("readable")),
        writeable=bool(entry.get("writeable")),
        executable=bool(entry.get("executable") or iid.startswith("action.")),
        unit=_optional_str(entry.get("unit")),
        value_list=entry.get("value_list") if isinstance(entry.get("value_list"), list) else None,
        value_range=entry.get("value_range") if isinstance(entry.get("value_range"), list) else None,
    )


def _merge_areas(
    areas: list[dict[str, object]],
    devices: list[UnifiedDeviceInfo],
) -> list[dict[str, object]]:
    names = {
        str(area.get("name"))
        for area in areas
        if isinstance(area, dict) and area.get("name")
    }
    for device in devices:
        if device.room:
            names.add(device.room)
    return [{"name": name} for name in sorted(names)]


def _normalize_areas(raw: object) -> list[dict[str, object]]:
    if not isinstance(raw, list):
        return []
    result: list[dict[str, object]] = []
    for item in raw:
        if isinstance(item, dict):
            name = item.get("name")
            if name:
                result.append({"name": str(name)})
        elif isinstance(item, str):
            result.append({"name": item})
    return result


def _optional_str(value: object) -> str | None:
    return str(value) if value is not None else None

