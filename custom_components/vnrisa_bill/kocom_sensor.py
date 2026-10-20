"""kshexe/kocom_energy의 sensor.py를 그대로 포팅 - 코콤 월패드에서 받아온 이번달
전기·가스·수도 사용량을 센서로 만든다(온수/난방은 이 집엔 해당 없어 - 항상 0 - 아예
센서로 안 만든다). 로직(특히 전기 센서의 '이번달 사용량이 이전 응답의 지난달 사용량과
같으면 월경계 글리치로 보고 버리는' 검증)은 그대로 가져왔고, device_info만 이 통합의
공용 기기("엘크루 공과금")를 쓰도록 바꿨다.

vnrisa 관리비/GSE 가스와 달리 자체 DataUpdateCoordinator로 폴링한다(원본 그대로) -
아파트 단지 로컬 소켓이라 원래도 Token 문제가 없고, 주기적 폴링이 자연스러운 방식이다.
"""
from __future__ import annotations

from collections import defaultdict
import datetime
import logging
import math
from datetime import timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import CONF_KOCOM_IP, CONF_KOCOM_PASSWORD, CONF_KOCOM_UPDATE_INTERVAL, CONF_KOCOM_USERNAME, DOMAIN
from .kocom_api import KocomAPI

_LOGGER = logging.getLogger(__name__)

# 이 집에서 코콤/엘크루 공과금이 생기기 전부터(2024년초~) 전기 사용량을 따로 추적하던
# 더 오래된 엔티티 - 가스/수도엔 이런 긴 과거 기록이 없어 전기만 해당. 한 번만
# history에 심어주는 용도라, 이 통계가 없는 환경(다른 설치)에서는 조용히 그냥 건너뛴다.
_OLD_ELECTRICITY_STATISTIC_ID = "sensor.jeongiyogeum_kwhto_kwh"


def _month_offset(month: str, delta_months: int) -> str:
    """'YYYY-MM' 문자열에서 delta_months만큼 이동한 'YYYY-MM'을 돌려준다(음수=과거)."""
    year, mon = int(month[:4]), int(month[5:7])
    total = year * 12 + (mon - 1) + delta_months
    y, m = divmod(total, 12)
    return f"{y:04d}-{m + 1:02d}"


async def _seed_electricity_history(hass: HomeAssistant) -> dict[str, float]:
    """_OLD_ELECTRICITY_STATISTIC_ID의 일별 장기통계에서 월별 사용량을 복원한다. 기기가
    재연결되며 리셋된 날은 하루치에 음수 delta가 몰려 찍히는데, 그 날짜 하루만 버리고
    (그 달 전체를 버리지 않고) 나머지로 월별 합을 만든다."""
    start = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    try:
        rows = await get_instance(hass).async_add_executor_job(
            statistics_during_period, hass, start, None, {_OLD_ELECTRICITY_STATISTIC_ID}, "day", None, {"change"}
        )
    except Exception as e:  # noqa: BLE001
        _LOGGER.debug("과거 전기 사용량 통계 복원 건너뜀: %s", e)
        return {}

    monthly: dict[str, float] = defaultdict(float)
    for row in rows.get(_OLD_ELECTRICITY_STATISTIC_ID, []):
        change = row.get("change")
        if change is None or change < 0:
            continue
        start_dt = datetime.datetime.fromtimestamp(row["start"] / 1000, tz=datetime.timezone.utc)
        monthly[start_dt.strftime("%Y-%m")] += change
    return dict(monthly)

# 전기/가스/수도만 상단에 바로 보이고, 나머지(진단용 조회시각)는 진단 카테고리로
# 내려간다 - vnrisa/GSE 쪽과 같은 기준.
_PROMINENT_TYPES = {"electricity", "gas", "water"}

SENSOR_TYPES = {
    "energy": {
        # 값이 "2026-10-19 19:41:00" 같은 문자열이라 state_class를 주면 HA가 숫자형이
        # 아니라고 엔티티 추가 자체를 거부한다(최신 HA에서 강화된 검증) - 원본
        # kocom_energy에 있던 state_class: measurement를 빼서 고쳤다.
        "name": "코콤 에너지 조회시각",
        "device_class": "",
        "unit_of_measurement": "",
        "state_class": "",
        "icon": "mdi:api",
    },
    "electricity": {
        "name": "전기 사용량",
        "device_class": "energy",
        "unit_of_measurement": "kWh",
        "state_class": "total_increasing",
        "icon": "mdi:flash",
    },
    "gas": {
        "name": "가스 사용량",
        "device_class": "gas",
        "unit_of_measurement": "m³",
        "state_class": "total_increasing",
        "icon": "mdi:fire",
    },
    "water": {
        "name": "수도 사용량",
        "device_class": "water",
        "unit_of_measurement": "m³",
        "state_class": "total_increasing",
        "icon": "mdi:water",
    },
}


def _device_info(entry: ConfigEntry) -> dict:
    return {
        "identifiers": {(DOMAIN, entry.entry_id)},
        "name": "엘크루 공과금",
        "manufacturer": "vnrisa (비공식) / GSE 도시가스 / Kocom",
        "model": "관리비·가스·코콤 고지서",
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

    sensors = [
        KocomEnergySensor(coordinator, entry, sensor_type, sensor_data)
        for sensor_type, sensor_data in SENSOR_TYPES.items()
    ]
    async_add_entities(sensors)


class KocomEnergySensor(CoordinatorEntity, RestoreEntity, SensorEntity):
    """원본 KocomEnergySensor와 동일한 로직 - device_info만 공용 기기로 변경하고,
    전기/가스/수도 사용량엔 월별 history(+전월/작년 속성)를 추가했다."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry, sensor_type: str, sensor_data: dict) -> None:
        super().__init__(coordinator)

        self._sensor_type = sensor_type
        self._attr_name = sensor_data["name"]
        self._attr_unique_id = f"{entry.entry_id}_kocom_{sensor_type}"
        self._attr_device_class = sensor_data["device_class"] or None
        self._attr_native_unit_of_measurement = sensor_data["unit_of_measurement"] or None
        self._attr_state_class = sensor_data["state_class"] or None
        self._attr_icon = sensor_data["icon"]
        self._attr_device_info = _device_info(entry)
        self._attr_entity_category = None if sensor_type in _PROMINENT_TYPES else EntityCategory.DIAGNOSTIC

        # 정상 데이터가 수신될 때 이번달/지난달 전기 사용량 보관용 변수 (월경계 글리치 검증용)
        self._previous_electricity_usage_this_month = None
        self._previous_electricity_this_month = None
        self._previous_electricity_usage_last_month = None
        self._previous_electricity_last_month = None

        # 완료된 달의 사용량 - 코콤이 매 조회마다 알려주는 last_month/two_months_ago를
        # 월별로 쌓아서 history로 만든다. 전기만 추가로 2년치를 한 번 복원해온다.
        self._history: dict[str, float] = {}
        self._history_seeded = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._sensor_type not in _PROMINENT_TYPES:
            return

        last_state = await self.async_get_last_state()
        if last_state is not None:
            attrs = last_state.attributes or {}
            for month, value in dict(attrs.get("history", {}) or {}).items():
                self._history.setdefault(month, value)
            self._history_seeded = bool(attrs.get("history_seeded", False))

        if self._sensor_type == "electricity" and not self._history_seeded:
            seeded = await _seed_electricity_history(self.hass)
            for month, value in seeded.items():
                self._history.setdefault(month, round(value, 1))
            self._history_seeded = True
            self.async_write_ha_state()

    def _handle_coordinator_update(self) -> None:
        if self._sensor_type in _PROMINENT_TYPES and self.coordinator.data:
            for month_key, usage_key in (
                ("last_month", f"{self._sensor_type}_usage_last_month"),
                ("two_months_ago", f"{self._sensor_type}_usage_two_months_ago"),
            ):
                month = self.coordinator.data.get(month_key)
                usage = self.coordinator.data.get(usage_key)
                if month and usage is not None:
                    self._history[month] = usage
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
                return current_usage

            elif self._sensor_type == "gas":
                current_usage = self.coordinator.data.get("gas_usage_this_month")
            elif self._sensor_type == "water":
                current_usage = self.coordinator.data.get("water_usage_this_month")
        except Exception as e:  # noqa: BLE001
            _LOGGER.error("%s 센서 데이터 처리 중 오류 발생: %s", self._sensor_type, e)
            return None

        return current_usage

    @property
    def extra_state_attributes(self) -> dict:
        if self._sensor_type == "energy":
            return self.coordinator.data or {}
        if self._sensor_type not in _PROMINENT_TYPES:
            return {}

        attrs: dict = {"history": dict(sorted(self._history.items()))}
        if self._sensor_type == "electricity":
            attrs["history_seeded"] = self._history_seeded
        this_month = (self.coordinator.data or {}).get("this_month")
        if this_month:
            last_m = _month_offset(this_month, -1)
            year_m = _month_offset(this_month, -12)
            if last_m in self._history:
                attrs["전월"] = self._history[last_m]
            if year_m in self._history:
                attrs["작년"] = self._history[year_m]
        return attrs
