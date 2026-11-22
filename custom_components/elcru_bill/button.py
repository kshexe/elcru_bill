"""고지서 조회 버튼: text.py의 "고지서 링크" 입력칸에 붙여넣은 값을 실제 조회로 넘긴다.

text.py는 더 이상 입력 즉시 조회하지 않는다(2026-11-22) - 이 버튼을 눌러야 그 시점에
입력칸에 들어있는 값으로 async_refresh_bill이 돈다. 조회를 시도한 뒤에는 성공/실패와
무관하게 입력칸을 빈 값으로 되돌린다 - 지난달 링크가 그대로 남아있으면 "이미 확인
했다"고 착각하기 쉬워서, 다음 달엔 반드시 새 링크를 다시 붙여넣게 한다.
"""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import VnrisaConfigEntry, async_refresh_bill
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _device_info(entry: ConfigEntry) -> dict:
    return {
        "identifiers": {(DOMAIN, entry.entry_id)},
        "name": "엘크루 관리비",
        "manufacturer": "vnrisa / GSE / citywaste / Kocom",
        "model": "관리비·가스·음쓰·코콤",
    }


async def async_setup_entry(
    hass: HomeAssistant, entry: VnrisaConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([VnrisaLinkConfirmButton(entry)])


class VnrisaLinkConfirmButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_name = "고지서 조회"
    _attr_icon = "mdi:check-circle-outline"

    def __init__(self, entry: VnrisaConfigEntry) -> None:
        self._entry = entry
        self._runtime = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_link_confirm"
        self._attr_device_info = _device_info(entry)

    async def async_press(self) -> None:
        url = (self._runtime.url or "").strip()
        if url:
            await async_refresh_bill(self.hass, self._entry, url)
        else:
            _LOGGER.warning("고지서 조회 버튼을 눌렀지만 입력된 링크가 없음")

        text_entity = self._runtime.text_entity
        if text_entity is not None:
            text_entity._attr_native_value = ""
            text_entity.async_write_ha_state()
        self._runtime.url = ""
