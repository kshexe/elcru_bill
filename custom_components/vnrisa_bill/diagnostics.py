"""설정 > 통합 > 엘크루 관리비 > ⋮ > 진단 다운로드에서 바로 상태를 확인할 수 있게.
로그인 정보(아이디/비밀번호/해시값/서버 IP)는 전부 레다크(가림) 처리한다."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import VnrisaConfigEntry

TO_REDACT = {
    "gas_member_id",
    "gas_member_pwd",
    "kocom_username",
    "kocom_password",
    "kocom_fcm",
    "kocom_phone",
    "kocom_original_username",
    "kocom_ip",
    "citywaste_id",
    "citywaste_pwd",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: VnrisaConfigEntry
) -> dict[str, Any]:
    runtime = entry.runtime_data
    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "entry_options": dict(entry.options),
        "bill": {
            "last_error": runtime.error,
            "sensor_count": len(runtime.item_sensors),
            "latest_fetched_month": (runtime.data or {}).get("month"),
        },
        "gas": {
            "last_error": runtime.gas_error,
            "sensor_count": len(runtime.gas_sensors),
        },
        "citywaste": {
            "last_error": runtime.citywaste_error,
            "sensor_count": len(runtime.citywaste_sensors),
        },
    }
