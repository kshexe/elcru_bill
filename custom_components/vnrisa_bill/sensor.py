"""고지서의 모든 항목을 센서로 노출. 새 항목이 생기면 자동으로 추가된다."""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import DOMAIN
from .coordinator import VnrisaCoordinator

WON = "원"
METER_UNITS = {"전기": ("kWh", SensorDeviceClass.ENERGY)}
METER_DEFAULT = ("m³", SensorDeviceClass.WATER)


def _spec_list(data: dict[str, Any]) -> list[dict[str, Any]]:
    """data → 센서 정의 목록. key는 unique_id 접미사."""
    specs: list[dict[str, Any]] = []

    for label in data["summary"]:
        specs.append({"key": f"summary_{label}", "name": label, "kind": "won",
                      "get": lambda d, l=label: d["summary"].get(l)})
    for label in data["details"]:
        specs.append({"key": f"detail_{label}", "name": label, "kind": "won",
                      "get": lambda d, l=label: d["details"].get(l)})
    for label in data["charges"]:
        specs.append({"key": f"charge_{label}", "name": label, "kind": "won",
                      "get": lambda d, l=label: (d["charges"].get(l) or {}).get("value"),
                      "attrs": lambda d, l=label: {
                          "전월대비": (d["charges"].get(l) or {}).get("prev_diff"),
                          "전년동월대비": (d["charges"].get(l) or {}).get("yoy_diff")}})
    for label, u in data["usage"].items():
        specs.append({"key": f"usage_{label}", "name": f"{label} 사용량", "kind": "usage",
                      "unit": u["unit"],
                      "get": lambda d, l=label: (d["usage"].get(l) or {}).get("value")})
    for label in data["meters"]:
        specs.append({"key": f"meter_{label}", "name": f"{label} 계량기 지침", "kind": "meter",
                      "meter": label,
                      "get": lambda d, l=label: (d["meters"].get(l) or {}).get("당월"),
                      "attrs": None})
    return specs


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: VnrisaCoordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        if not coordinator.data:
            return
        new: list[SensorEntity] = []
        if "month" not in known:
            known.update({"month", "due_date"})
            new += [VnrisaMonthSensor(coordinator, entry), VnrisaDueDateSensor(coordinator, entry)]
        for spec in _spec_list(coordinator.data):
            if spec["key"] not in known:
                known.add(spec["key"])
                new.append(VnrisaSensor(coordinator, entry, spec))
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class _Base(CoordinatorEntity[VnrisaCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: VnrisaCoordinator, entry: ConfigEntry, key: str, name: str = ""
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.room_key}_{key}"
        self.entity_id = f"sensor.gwanribi_{slugify(name or key)}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.room_key)},
            name=entry.title,
            manufacturer="vnrisa 스마트 고지서",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=(coordinator.data or {}).get("url"),
        )


class VnrisaSensor(_Base):
    def __init__(self, coordinator, entry, spec: dict[str, Any]) -> None:
        super().__init__(coordinator, entry, spec["key"], spec["name"])
        self._get: Callable = spec["get"]
        self._attrs: Callable | None = spec.get("attrs")
        self._attr_name = spec["name"]
        kind = spec["kind"]
        if kind == "won":
            self._attr_native_unit_of_measurement = WON
            self._attr_icon = "mdi:cash"
        elif kind == "usage":
            self._attr_native_unit_of_measurement = spec["unit"]
            self._attr_icon = "mdi:flash" if spec["unit"] == "kWh" else "mdi:water"
        else:  # meter: 월별 기록은 통계(vnrisa_bill:…)로 따로 들어감
            unit, dc = METER_UNITS.get(spec["meter"], METER_DEFAULT)
            self._attr_native_unit_of_measurement = unit
            self._attr_device_class = dc

    @property
    def native_value(self):
        return self._get(self.coordinator.data) if self.coordinator.data else None

    @property
    def extra_state_attributes(self):
        d = self.coordinator.data or {}
        attrs: dict[str, Any] = {"청구월": d.get("month")}
        if self._attrs and d:
            attrs.update(self._attrs(d))
        hist = self.coordinator.history.get(self._attr_name, {})
        for ym in sorted(hist, reverse=True):  # 최근 월이 위로
            attrs[ym] = hist[ym]
        return attrs


class VnrisaMonthSensor(_Base):
    _attr_name = "청구월"
    _attr_icon = "mdi:calendar-month"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "info_month")
        self.entity_id = "sensor.gwanribi_cheonggueol"

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("month")

    @property
    def extra_state_attributes(self):
        d = self.coordinator.data or {}
        return {"url": d.get("url"), "apartment": d.get("apartment"), "unit": d.get("unit")}


class VnrisaDueDateSensor(_Base):
    _attr_name = "납부마감일"
    _attr_device_class = SensorDeviceClass.DATE

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "info_due_date")
        self.entity_id = "sensor.gwanribi_nabbumagamil"

    @property
    def native_value(self):
        v = (self.coordinator.data or {}).get("due_date")
        return date.fromisoformat(v) if v else None
