"""하루 1회 고지서를 받아와 파싱하고, 청구월이 바뀌면 이벤트를 발생시킨다."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import BASE_URL, CONF_ROOM_KEY, DOMAIN, EVENT_NEW_BILL
from .parser import parse_bill
from .stats import import_monthly

HISTORY_MONTHS = 24

_LOGGER = logging.getLogger(__name__)


async def fetch_bill(hass: HomeAssistant, room_key: str) -> dict[str, Any]:
    """RoomKey만 보내면 서버가 302로 최신 고지서(유효 Token)로 보내준다."""
    session = async_get_clientsession(hass)
    url = BASE_URL.format(room_key=room_key)
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        resp.raise_for_status()
        html = await resp.text(encoding="utf-8", errors="replace")
        final_url = str(resp.url)
    data = await hass.async_add_executor_job(parse_bill, html)
    data["url"] = final_url
    return data


class VnrisaCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, _LOGGER, name=DOMAIN, config_entry=entry, update_interval=None
        )
        self.room_key: str = entry.data[CONF_ROOM_KEY]
        self._store: Store = Store(hass, 1, f"{DOMAIN}.{self.room_key}")
        self._last_month: str | None = None
        self._baselines: dict[str, float] = {}
        self.history: dict[str, dict[str, Any]] = {}
        self._loaded = False

    async def _async_update_data(self) -> dict[str, Any]:
        if not self._loaded:
            stored = await self._store.async_load() or {}
            self._last_month = stored.get("last_month")
            self._baselines = stored.get("baselines", {})
            self.history = stored.get("history", {})
            self._loaded = True

        try:
            data = await fetch_bill(self.hass, self.room_key)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(f"고지서 조회 실패: {err}") from err

        month = data.get("month")
        if not month:
            raise UpdateFailed("고지서에서 청구월을 찾지 못함 (페이지 구조 변경?)")

        before = dict(self._baselines)
        import_monthly(self.hass, data, self._baselines)
        dirty = before != self._baselines
        dirty |= self._update_history(data)

        if month != self._last_month:
            dirty = True
            if self._last_month is not None:
                self.hass.bus.async_fire(
                    EVENT_NEW_BILL,
                    {
                        "room_key": self.room_key,
                        "month": month,
                        "previous_month": self._last_month,
                        "due_date": data.get("due_date"),
                        "url": data.get("url"),
                        **data["summary"],
                    },
                )
            self._last_month = month
        if dirty:
            await self._store.async_save(
                {
                    "last_month": self._last_month,
                    "baselines": self._baselines,
                    "history": self.history,
                }
            )
        return data

    def _update_history(self, data: dict[str, Any]) -> bool:
        """센서 이름별 {청구월: 값} 기록. 당월 값은 덮어쓰고, 전월/전년 값은 비어 있을 때만 채운다."""
        ym = data["month"]
        series: dict[str, dict[int, Any]] = {}
        for name, v in {**data["summary"], **data["details"]}.items():
            series[name] = {0: v}
        for name, c in data["charges"].items():
            series[name] = {0: c.get("value"), -1: c.get("prev"), -12: c.get("yoy")}
        for name, u in data["usage"].items():
            series[f"{name} 사용량"] = {0: u.get("value")}
        for name, m in data["meters"].items():
            series[f"{name} 계량기 지침"] = {0: m.get("당월"), -1: m.get("전월")}

        changed = False
        for name, points in series.items():
            hist = self.history.setdefault(name, {})
            for shift, value in points.items():
                if not isinstance(value, (int, float)):
                    continue
                key = shift_month(ym, shift)
                if shift == 0 or key not in hist:
                    if hist.get(key) != value:
                        hist[key] = value
                        changed = True
            if len(hist) > HISTORY_MONTHS:
                for old in sorted(hist)[:-HISTORY_MONTHS]:
                    del hist[old]
                changed = True
        return changed


def shift_month(ym: str, shift: int) -> str:
    y, m = (int(x) for x in ym.split("-"))
    idx = y * 12 + (m - 1) + shift
    return f"{idx // 12}-{idx % 12 + 1:02d}"
