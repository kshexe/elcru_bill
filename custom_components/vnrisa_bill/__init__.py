"""아파트 스마트 고지서 (vnrisa) 통합."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.event import async_track_time_change

from .const import CONF_CHECK_TIME, DEFAULT_CHECK_TIME, DOMAIN, PLATFORMS
from .coordinator import VnrisaCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = VnrisaCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    check = entry.options.get(CONF_CHECK_TIME, entry.data.get(CONF_CHECK_TIME, DEFAULT_CHECK_TIME))
    hour, minute = (int(x) for x in check.split(":"))

    async def _scheduled(_now) -> None:
        await coordinator.async_request_refresh()

    entry.async_on_unload(
        async_track_time_change(hass, _scheduled, hour=hour, minute=minute, second=0)
    )
    entry.async_on_unload(entry.add_update_listener(_reload))

    if not hass.services.has_service(DOMAIN, "refresh"):
        async def _refresh(call: ServiceCall) -> None:
            for e in hass.config_entries.async_loaded_entries(DOMAIN):
                await e.runtime_data.async_request_refresh()

        hass.services.async_register(DOMAIN, "refresh", _refresh)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
