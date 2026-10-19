from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

from .const import CONF_CHECK_TIME, CONF_ROOM_KEY, DEFAULT_CHECK_TIME, DOMAIN
from .coordinator import fetch_bill

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def _extract_room_key(value: str) -> str | None:
    value = value.strip()
    if m := re.search(r"RoomKey=(\d+)", value, re.I):
        return m.group(1)
    return value if value.isdigit() else None


class VnrisaConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            room_key = _extract_room_key(user_input[CONF_ROOM_KEY])
            check_time = user_input[CONF_CHECK_TIME].strip()
            if not room_key:
                errors[CONF_ROOM_KEY] = "invalid_room_key"
            elif not _TIME_RE.match(check_time):
                errors[CONF_CHECK_TIME] = "invalid_time"
            else:
                await self.async_set_unique_id(room_key)
                self._abort_if_unique_id_configured()
                try:
                    data = await fetch_bill(self.hass, room_key)
                except Exception:  # noqa: BLE001
                    errors["base"] = "cannot_connect"
                else:
                    if not data.get("month") or not data.get("charges"):
                        errors["base"] = "parse_failed"
                    else:
                        title = " ".join(x for x in (data.get("apartment"), data.get("unit")) if x)
                        return self.async_create_entry(
                            title=title or f"관리비 {room_key}",
                            data={CONF_ROOM_KEY: room_key, CONF_CHECK_TIME: check_time},
                        )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ROOM_KEY): str,
                    vol.Required(CONF_CHECK_TIME, default=DEFAULT_CHECK_TIME): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return VnrisaOptionsFlow()


class VnrisaOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current = self.config_entry.options.get(
            CONF_CHECK_TIME, self.config_entry.data.get(CONF_CHECK_TIME, DEFAULT_CHECK_TIME)
        )
        if user_input is not None:
            if _TIME_RE.match(user_input[CONF_CHECK_TIME].strip()):
                return self.async_create_entry(
                    data={CONF_CHECK_TIME: user_input[CONF_CHECK_TIME].strip()}
                )
            errors[CONF_CHECK_TIME] = "invalid_time"
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Required(CONF_CHECK_TIME, default=current): str}),
            errors=errors,
        )
