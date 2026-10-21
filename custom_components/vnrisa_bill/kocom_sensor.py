"""kshexe/kocom_energy의 sensor.py를 그대로 포팅 - 코콤 월패드에서 받아온 이번달
전기·가스·수도 사용량을 센서로 만든다(온수/난방은 이 집엔 해당 없어 - 항상 0 - 아예
센서로 안 만든다). 로직(특히 전기 센서의 '이번달 사용량이 이전 응답의 지난달 사용량과
같으면 월경계 글리치로 보고 버리는' 검증)은 그대로 가져왔고, device_info만 이 통합의
공용 기기("엘크루 관리비")를 쓰도록 바꿨다.

vnrisa 관리비/GSE 가스와 달리 자체 DataUpdateCoordinator로 폴링한다(원본 그대로) -
아파트 단지 로컬 소켓이라 원래도 Token 문제가 없고, 주기적 폴링이 자연스러운 방식이다.
"""
from __future__ import annotations

import datetime
import logging
import math
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator
from homeassistant.util import slugify

from .const import CONF_KOCOM_IP, CONF_KOCOM_PASSWORD, CONF_KOCOM_UPDATE_INTERVAL, CONF_KOCOM_USERNAME, DOMAIN
from .kocom_api import KocomAPI

_LOGGER = logging.getLogger(__name__)

# 코콤도, 위 과거 통계도 둘 다 비어있는 달(그 시기 자체에 기기가 꺼져있던 경우, 예:
# 2026-04)은 vnrisa 관리비 고지서 쪽 전기 사용량으로 메운다 - 거긴 코콤과 무관하게
# 그 달 고지서를 붙여넣을 때마다 따로 쌓인다. vnrisa는 2026-03부터만 있어서 그보다
# 오래된 공백(예: 2025-09)은 이것으로도 못 채운다.
_VNRISA_ELECTRICITY_KEY = "전기 사용량"  # sensor.py의 VnrisaItemSensor가 쓰는 "안정적인 key"


def _supplement_from_vnrisa(hass: HomeAssistant, entry_id: str, history: dict[str, Any]) -> None:
    """entity_id를 그대로 박아두면 기기/엔티티 이름이 바뀔 때(has_entity_name이 이름
    바뀌면 entity_id도 따라 바꿈 - 실제로 "엘크루 공과금"→"관리비" 때 한 번 조용히
    끊긴 적 있음) 깨진다. unique_id(=entry_id + slugify(안정적인 key))는 안 바뀌니
    그걸로 레지스트리에서 매번 다시 찾는다."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry_id}_{slugify(_VNRISA_ELECTRICITY_KEY)}")
    if entity_id is None:
        return
    state = hass.states.get(entity_id)
    if state is None:
        return
    for month, value in (state.attributes.get("history") or {}).items():
        history.setdefault(month, _round_half_up(value))


def _normalize_month(raw: str | None) -> str | None:
    """코콤이 this_month/last_month/two_months_ago로 주는 건 'YYYYMM'(하이픈 없음) -
    history 쪽(vnrisa/GSE)은 전부 'YYYY-MM'을 쓰므로 여기서 맞춰준다. 포맷이
    다르면(응답 깨짐 등) None."""
    if not raw or len(raw) != 6 or not raw.isdigit():
        return None
    return f"{raw[:4]}-{raw[4:]}"


def _month_offset(month: str, delta_months: int) -> str:
    """'YYYY-MM' 문자열에서 delta_months만큼 이동한 'YYYY-MM'을 돌려준다(음수=과거)."""
    year, mon = int(month[:4]), int(month[5:7])
    total = year * 12 + (mon - 1) + delta_months
    y, m = divmod(total, 12)
    return f"{y:04d}-{m + 1:02d}"


def _round_half_up(value: float | None) -> int | None:
    """예전 "...(반올림)" 도우미들과 같은 반올림 방식(음수 없는 사용량 전제) - 코콤이
    소수점 1자리까지 주는 값을 정수로 보여준다. history 자체는 원래 정밀도를 유지하고,
    화면에 보이는 값만 반올림한다."""
    return int(value + 0.5) if value is not None else None


def _prune_history(history: dict[str, Any], this_month: str, keep_months: int = 12) -> None:
    """"작년 같은 달"까지만 보면 되니(지금이 10월이면 작년 10월까지), 12개월이면 충분해서
    그보다 오래된 기록은 계속 쌓아두지 않고 지운다."""
    cutoff = _month_offset(this_month, -keep_months)
    for month in [m for m in history if m < cutoff]:
        del history[month]


def _nearest_year_ago(history: dict[str, Any], this_month: str) -> Any | None:
    """정확히 "작년 같은 달"이 있으면 그걸 쓰고, 없을 때만(데이터 공백) 기존 "전기
    작년사용량" SQL 도우미처럼 11개월 전 이전 중 가장 최근 값으로 대체한다."""
    exact = history.get(_month_offset(this_month, -12))
    if exact is not None:
        return exact
    threshold = _month_offset(this_month, -11)
    candidates = [m for m in history if m <= threshold]
    return history[max(candidates)] if candidates else None


# 전기/가스/수도만 상단에 바로 보이고, 나머지(진단용 조회시각)는 진단 카테고리로
# 내려간다 - vnrisa/GSE 쪽과 같은 기준.
_PROMINENT_TYPES = {"electricity", "gas", "water"}

SENSOR_TYPES = {
    "energy": {
        # 값이 "2026-10-19 19:41:00" 같은 문자열이라 state_class를 주면 HA가 숫자형이
        # 아니라고 엔티티 추가 자체를 거부한다(최신 HA에서 강화된 검증) - 원본
        # kocom_energy에 있던 state_class: measurement를 빼서 고쳤다.
        "name": "코콤 조회시각 (kocom)",
        "device_class": "",
        "unit_of_measurement": "",
        "state_class": "",
        "icon": "mdi:clock-check-outline",
    },
    "electricity": {
        "name": "전기 사용량 (kocom)",
        "device_class": "",
        "unit_of_measurement": "",
        "state_class": "total_increasing",
        "icon": "mdi:flash",
    },
    "gas": {
        "name": "가스 사용량 (kocom)",
        "device_class": "",
        "unit_of_measurement": "",
        "state_class": "total_increasing",
        "icon": "mdi:fire",
    },
    "water": {
        "name": "수도 사용량 (kocom)",
        "device_class": "",
        "unit_of_measurement": "",
        "state_class": "total_increasing",
        "icon": "mdi:water",
    },
}


def _device_info(entry: ConfigEntry) -> dict:
    return {
        "identifiers": {(DOMAIN, entry.entry_id)},
        "name": "엘크루 관리비",
        "manufacturer": "vnrisa / GSE / Kocom / citywaste",
        "model": "관리비·가스·코콤·음쓰",
    }


async def async_setup_kocom_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """entry.data에 코콤 로그인 정보가 있을 때만 코콤 센서를 설치한다."""
    if not entry.data.get(CONF_KOCOM_IP):
        return

    update_interval = entry.data.get(CONF_KOCOM_UPDATE_INTERVAL)
    issue_id = f"{entry.entry_id}_kocom_auth_failed"

    async def async_update_kocom_energy():
        _LOGGER.info("==================== 코콤 센서 업데이트 시작 ====================")
        try:
            api = KocomAPI(
                ip=entry.data.get(CONF_KOCOM_IP),
                username=entry.data.get(CONF_KOCOM_USERNAME),
                password=entry.data.get(CONF_KOCOM_PASSWORD),
                fcm=entry.data.get("kocom_fcm", ""),
                phone=entry.data.get("kocom_phone", ""),
            )
            energy_response_dict = await api.get_energy_data()
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _LOGGER.info("코콤 센서 업데이트, 시각: %s, 응답: %s", now, energy_response_dict)
            ir.async_delete_issue(hass, DOMAIN, issue_id)
            return energy_response_dict
        except Exception as e:  # noqa: BLE001
            # 로그에만 남기면 조용히 계속 실패해도 못 알아채서, 설정 > 수리 항목으로
            # 바로 눈에 띄게 한다 - 다음 조회가 성공하면 위에서 자동으로 지워진다.
            _LOGGER.error("코콤 센서 업데이트 오류: %s", e)
            ir.async_create_issue(
                hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="kocom_auth_failed",
                translation_placeholders={"error": str(e)},
            )
            return {}

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="kocom_energy",
        update_method=async_update_kocom_energy,
        update_interval=timedelta(seconds=update_interval) if update_interval else None,
    )

    await coordinator.async_config_entry_first_refresh()

    main_sensors = {
        sensor_type: KocomEnergySensor(coordinator, entry, sensor_type, sensor_data)
        for sensor_type, sensor_data in SENSOR_TYPES.items()
    }
    history_sensors = [
        KocomUsageHistorySensor(coordinator, main_sensors[sensor_type], entry, sensor_type, kind)
        for sensor_type in _PROMINENT_TYPES
        for kind in ("last_month", "year_ago")
    ]
    async_add_entities([*main_sensors.values(), *history_sensors])


class KocomEnergySensor(CoordinatorEntity, RestoreEntity, SensorEntity):
    """원본 KocomEnergySensor와 동일한 로직 - device_info만 공용 기기로 변경하고,
    전기/가스/수도 사용량엔 월별 history(+전월/작년 속성)를 추가했다."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry, sensor_type: str, sensor_data: dict) -> None:
        super().__init__(coordinator)

        self._entry_id = entry.entry_id
        self._sensor_type = sensor_type
        self._attr_name = sensor_data["name"]
        self._attr_unique_id = f"{entry.entry_id}_kocom_{sensor_type}"
        self._attr_device_class = sensor_data["device_class"] or None
        self._attr_native_unit_of_measurement = sensor_data["unit_of_measurement"] or None
        self._attr_state_class = sensor_data["state_class"] or None
        self._attr_icon = sensor_data["icon"]
        self._attr_device_info = _device_info(entry)
        self._attr_entity_category = None if sensor_type in _PROMINENT_TYPES else EntityCategory.DIAGNOSTIC
        if sensor_type in _PROMINENT_TYPES:
            # 값 자체를 반올림해서 정수로 주지만, 프론트엔드가 device_class 기준으로
            # 소수점을 다시 붙여서 보여줄 수 있어 표시 정밀도도 0으로 고정한다.
            self._attr_suggested_display_precision = 0

        # 정상 데이터가 수신될 때 이번달/지난달 전기 사용량 보관용 변수 (월경계 글리치 검증용)
        self._previous_electricity_usage_this_month = None
        self._previous_electricity_this_month = None
        self._previous_electricity_usage_last_month = None
        self._previous_electricity_last_month = None

        # 완료된 달의 사용량 - 코콤이 매 조회마다 알려주는 last_month/two_months_ago를
        # 월별로 쌓아서 history로 만든다(한 번 1837.4처럼 튄 적이 있었지만, 연속 재조회로
        # 안정적인 걸 확인함 - 간단한 쪽으로 감). RestoreEntity로 재시작 때마다 복원되니
        # 코콤 자체가 최근 2~3달치만 알려줘도 그 전 기록이 계속 쌓인다.
        self._history: dict[str, float] = {}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._sensor_type not in _PROMINENT_TYPES:
            return

        last_state = await self.async_get_last_state()
        if last_state is not None:
            attrs = last_state.attributes or {}
            for month, value in dict(attrs.get("history", {}) or {}).items():
                # 예전 버전에서 소수점으로 저장된 값도 복원하면서 정수로 맞춘다.
                self._history.setdefault(month, _round_half_up(value))

        # coordinator.async_add_listener는 "앞으로의" 갱신에만 콜백을 걸어준다 - 엔티티가
        # 생성되기 전에 이미 받아온 첫 조회 데이터(coordinator.data)는 그냥 두면
        # _handle_coordinator_update가 다음 폴링(최대 1시간) 전까지 한 번도 안 불려서
        # history가 안 채워진다 - 지금 가진 데이터로 한 번 직접 돌려준다.
        self._handle_coordinator_update()

    def _handle_coordinator_update(self) -> None:
        if self._sensor_type in _PROMINENT_TYPES and self.coordinator.data:
            for month_key, usage_key in (
                ("last_month", f"{self._sensor_type}_usage_last_month"),
                ("two_months_ago", f"{self._sensor_type}_usage_two_months_ago"),
            ):
                month = _normalize_month(self.coordinator.data.get(month_key))
                usage = self.coordinator.data.get(usage_key)
                if month and usage is not None:
                    self._history[month] = _round_half_up(usage)
            if self._sensor_type == "electricity" and self.hass is not None:
                # vnrisa 엔티티는 플랫폼 설정 순서상 이 코콤 센서보다 늦게 만들어져서,
                # async_added_to_hass 시점엔 아직 없을 수 있다 - 매 폴링마다(또는
                # async_added_to_hass가 수동으로 한 번 불러주는 시점에) 다시 시도하면
                # 결국 한 번은 걸린다.
                _supplement_from_vnrisa(self.hass, self._entry_id, self._history)
            this_month = _normalize_month(self.coordinator.data.get("this_month"))
            if this_month:
                _prune_history(self._history, this_month)
        super()._handle_coordinator_update()

    @property
    def native_value(self):
        current_usage = None
        try:
            if not self.coordinator.data:
                if self._sensor_type == "energy":
                    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                return None

            if self._sensor_type == "energy":
                return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if self._sensor_type == "electricity":
                current_usage = self.coordinator.data.get("electricity_usage_this_month")
                this_month = self.coordinator.data.get("this_month")

                if (
                    self._previous_electricity_usage_last_month is not None
                    and self._previous_electricity_last_month is not None
                    and current_usage is not None
                    and this_month is not None
                ):
                    # 같은 달의 데이터인 경우에만 비교(정상적으로 월이 바뀌어 사용량이
                    # 0으로 초기화되는 경우는 제외)
                    if this_month == self._previous_electricity_this_month:
                        if math.isclose(current_usage, self._previous_electricity_usage_last_month):
                            _LOGGER.warning(
                                "전기 이번달 사용량이 이전 응답의 지난달 사용량과 동일함 "
                                "(이번달(%s): %s, 이전 지난달: %s)",
                                this_month, current_usage, self._previous_electricity_usage_last_month,
                            )
                            return None

                self._previous_electricity_usage_last_month = self.coordinator.data.get("electricity_usage_last_month")
                self._previous_electricity_last_month = self.coordinator.data.get("last_month")
                self._previous_electricity_usage_this_month = current_usage
                self._previous_electricity_this_month = this_month
                return _round_half_up(current_usage)

            elif self._sensor_type == "gas":
                current_usage = self.coordinator.data.get("gas_usage_this_month")
            elif self._sensor_type == "water":
                current_usage = self.coordinator.data.get("water_usage_this_month")
        except Exception as e:  # noqa: BLE001
            _LOGGER.error("%s 센서 데이터 처리 중 오류 발생: %s", self._sensor_type, e)
            return None

        return _round_half_up(current_usage)

    @property
    def extra_state_attributes(self) -> dict:
        if self._sensor_type == "energy":
            return self.coordinator.data or {}
        if self._sensor_type not in _PROMINENT_TYPES:
            return {}

        attrs: dict = {"history": dict(sorted(self._history.items()))}
        this_month = _normalize_month((self.coordinator.data or {}).get("this_month"))
        if this_month:
            last_m = _month_offset(this_month, -1)
            if last_m in self._history:
                attrs["전월"] = self._history[last_m]
            year_ago = _nearest_year_ago(self._history, this_month)
            if year_ago is not None:
                attrs["작년"] = year_ago
        return attrs


_HISTORY_KIND_LABELS = {"last_month": "전월사용량", "year_ago": "작년사용량"}
_HISTORY_KOCOM_LABELS = {"electricity": "전기", "gas": "가스", "water": "수도"}


class KocomUsageHistorySensor(CoordinatorEntity, SensorEntity):
    """KocomEnergySensor가 들고 있는 history에서 전월/작년 값만 뽑아 독립된 센서로
    노출한다 - 속성이 아니라 대시보드/그래프/자동화에서 바로 쓸 수 있게 하기 위함.
    값 자체는 source(=그 타입의 KocomEnergySensor)가 이미 쌓아둔 history를 그대로
    읽기만 하므로, 별도 폴링이나 자체 상태 저장이 필요 없다.

    전기/가스/수도 사용량(현재사용량) 3개와 같은 레벨의 "센서"로 바로 보이게 - 진단
    카테고리로 내려가지 않는다."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator, source: "KocomEnergySensor", entry: ConfigEntry, sensor_type: str, kind: str
    ) -> None:
        super().__init__(coordinator)
        self._source = source
        self._kind = kind
        label = _HISTORY_KIND_LABELS[kind]
        self._attr_name = f"{_HISTORY_KOCOM_LABELS[sensor_type]} {label} (kocom)"
        self._attr_unique_id = f"{entry.entry_id}_kocom_{sensor_type}_{kind}"
        self._attr_native_unit_of_measurement = source._attr_native_unit_of_measurement
        self._attr_icon = source._attr_icon
        self._attr_device_info = _device_info(entry)
        self._attr_suggested_display_precision = 0

    @property
    def native_value(self):
        this_month = _normalize_month((self.coordinator.data or {}).get("this_month"))
        if not this_month:
            return None
        history = self._source._history
        if self._kind == "last_month":
            return _round_half_up(history.get(_month_offset(this_month, -1)))
        return _round_half_up(_nearest_year_ago(history, this_month))
