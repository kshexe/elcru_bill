"""고지서 값을 '청구월' 기준 월별 장기 통계(external statistics)로 기록.

엔티티 상태는 조회한 날짜에 기록되므로(10월에 08월분 값) 월별 그래프가 어긋난다.
그래서 각 값을 해당 청구월 1일 00:00 시점의 통계로 직접 넣는다.
- 당월 값 → 청구월, 전월 값 → 청구월-1, 전년동월 값 → 청구월-12 (자동 백필)
- 계량기 지침은 sum으로 넣어 에너지 대시보드/통계 카드에서 월 사용량(change)으로 보인다.
같은 시점은 덮어쓰기(upsert)라 매일 조회해도 중복되지 않는다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util, slugify

from .const import DOMAIN

try:  # HA 2025.x+
    from homeassistant.components.recorder.models import StatisticMeanType
except ImportError:  # pragma: no cover
    StatisticMeanType = None  # type: ignore[assignment]

METER_UNITS = {"전기": "kWh"}


def statistic_id(name: str) -> str:
    return f"{DOMAIN}:{slugify(name)}"


def _month(ym: str, shift: int = 0) -> datetime:
    y, m = (int(x) for x in ym.split("-"))
    idx = y * 12 + (m - 1) + shift
    return datetime(idx // 12, idx % 12 + 1, 1, tzinfo=dt_util.get_default_time_zone())


def _meta(name: str, unit: str, *, total: bool) -> StatisticMetaData:
    meta: dict[str, Any] = {
        "source": DOMAIN,
        "statistic_id": statistic_id(name),
        "name": f"관리비 {name}",
        "unit_of_measurement": unit,
        "has_sum": total,
        "unit_class": None,
    }
    if StatisticMeanType is not None:
        meta["mean_type"] = StatisticMeanType.NONE if total else StatisticMeanType.ARITHMETIC
    else:
        meta["has_mean"] = not total
    return meta  # type: ignore[return-value]


def import_monthly(
    hass: HomeAssistant, data: dict[str, Any], baselines: dict[str, float]
) -> None:
    """baselines: 계량기별 최초 지침(sum=지침-기준값). 호출자가 저장한다."""
    ym = data.get("month")
    if not ym:
        return

    def add_mean(name: str, unit: str, points: dict[int, Any]) -> None:
        rows = [
            StatisticData(start=_month(ym, s), mean=v, min=v, max=v)
            for s, v in sorted(points.items()) if isinstance(v, (int, float))
        ]
        if rows:
            async_add_external_statistics(hass, _meta(name, unit, total=False), rows)

    for name, v in {**data["summary"], **data["details"]}.items():
        add_mean(name, "원", {0: v})
    for name, c in data["charges"].items():
        add_mean(name, "원", {0: c.get("value"), -1: c.get("prev"), -12: c.get("yoy")})
    for name, u in data["usage"].items():
        add_mean(f"{name} 사용량", u["unit"], {0: u.get("value")})

    for name, m in data["meters"].items():
        unit = METER_UNITS.get(name, "m³")
        first = m.get("전월", m.get("당월"))
        if not isinstance(first, (int, float)):
            continue
        base = baselines.setdefault(name, first)
        rows = [
            StatisticData(start=_month(ym, s), state=v, sum=v - base)
            for s, v in ((-1, m.get("전월")), (0, m.get("당월")))
            if isinstance(v, (int, float))
        ]
        if rows:
            async_add_external_statistics(
                hass, _meta(f"{name} 계량기 지침", unit, total=True), rows
            )
