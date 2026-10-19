"""고지서 링크: 카카오 알림톡의 "상세내역 조회" 버튼을 눌러서 연 관리비 고지서 주소.

값을 넣는 즉시 조회해서 sensor.py의 센서에 반영한다 - 별도 "저장"/"조회" 버튼 없음.
실제 조회+센서 반영+재시작용 저장 로직은 __init__.py의 async_refresh_bill에 있고,
여기선 그 함수를 호출만 한다(재시작 시 자동 재조회와 같은 코드를 쓰기 위함).

도시가스(GSE) 아이디/비밀번호는 여기가 아니라 통합 설정(최초 설치 시 config flow)에서
입력한다 - __init__.py의 async_refresh_gas가 그걸 읽어서 매일 자동으로 재조회한다.
"""
from __future__ import annotations

import logging

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import VnrisaConfigEntry, async_refresh_bill
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _device_info(entry: ConfigEntry) -> dict:
    return {
        "identifiers": {(DOMAIN, entry.entry_id)},
        "name": "엘크루 공과금",
        "manufacturer": "vnrisa (비공식) / GSE 도시가스 / Kocom",
        "model": "관리비·가스·코콤 고지서",
    }


async def async_setup_entry(
    hass: HomeAssistant, entry: VnrisaConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([VnrisaLinkText(entry)])


class VnrisaLinkText(TextEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_name = "고지서 링크"
    _attr_icon = "mdi:link-variant"
    _attr_native_min = 0
    _attr_native_max = 255

    def __init__(self, entry: VnrisaConfigEntry) -> None:
        self._entry = entry
        self._runtime = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_link"
        self._attr_native_value = ""
        self._attr_device_info = _device_info(entry)
        self._runtime.text_entity = self

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in (None, "unknown", "unavailable"):
            self._attr_native_value = last_state.state
            self._runtime.url = last_state.state
            # 입력창 표시값만 복원 - 실제 재조회는 __init__.py의 async_setup_entry가
            # url_store에 저장된 마지막 성공 링크로 따로 한 번 더 수행한다(Store가
            # 이 텍스트 엔티티의 RestoreEntity보다 먼저 읽히므로 중복이지만 안전함).

    async def async_set_value(self, value: str) -> None:
        value = value.strip()
        self._attr_native_value = value
        self._runtime.url = value
        self.async_write_ha_state()
        if value:
            await async_refresh_bill(self.hass, self._entry, value)
