"""text.py가 조회해온 고지서의 모든 항목을 항목명 하드코딩 없이 각자 센서로 만든다.

납부하실금액·당월부과금액 같은 요약 항목, 전기료/수도료/관리비 세부 항목과 소계,
기후환경요금 같은 할인/부담금, 전기·수도 사용량, 계량기 지침까지 - 고지서에 실제로
있는 항목만 그때그때 센서로 생긴다. 새 항목이 추가되면 자동으로 센서가 하나 더
생기고, 이미 있던 항목은 값만 갱신된다(엔티티를 지우진 않음).

더 최신 달 링크를 넣으면 평소대로 상태값이 그 달로 갱신되지만, 과거 달 링크를
나중에 넣으면(예: 8월 다음에 7월) 현재 보여주는 값(가장 최신 달)은 그대로 두고
`history` 속성에만 그 달 값을 추가한다 - 과거 데이터를 보려고 넣은 링크가 화면을
거꾸로 되돌리지 않도록.

엔티티의 unique_id는 항목의 "안정적인 key"(예: "전기료 소계")로만 정하고, 화면에
보이는 이름(표시명, 예: "전기료 소계 (vnrisa)")은 따로 둔다 - 출처 태그를 붙이거나
표시 문구를 나중에 또 바꾸더라도 entity_id가 안 바뀌어서 history가 안 날아간다.

프로미넌트(기본 카테고리, 기기 카드 상단)로 남는 건 코콤의 전기/가스/수도 사용량
3개(kocom_sensor.py)와 음쓰의 배출량/배출건수/최근배출량 3개(_CITYWASTE_PROMINENT_KEYS)
뿐이다 - 전부 실시간/실측값이라 가장 "지금" 상황을 보여주는 대표값으로 쓴다. vnrisa/GSE
쪽과 음쓰 정산금액/단가는 전부 진단으로 내려가고, 서로 이름이 겹치는 항목이 많아서(예:
전기 사용량이 vnrisa/코콤 양쪽에 있음) 전부 "(vnrisa)"/"(GSE)" 출처 태그를 붙인다.
"""
from __future__ import annotations

import re
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import slugify

from . import VnrisaConfigEntry
from .const import DOMAIN
from .kocom_sensor import async_setup_kocom_entry


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
    runtime = entry.runtime_data
    runtime.add_sensor_entities = async_add_entities

    runtime.bill_timestamp_sensor = VnrisaTimestampSensor(entry, "관리비 조회시각 (vnrisa)", "bill_timestamp")
    runtime.gas_timestamp_sensor = VnrisaTimestampSensor(entry, "도시가스 조회시각 (GSE)", "gas_timestamp")
    # 음쓰는 두 스케줄러가 서로 무관하게 따로 도는데(async_refresh_citywaste_live는
    # 월 감지와 무관하게 항상 30분마다, async_refresh_citywaste는 로그인까지 하는
    # 무거운 정산내역 확인이고 그 달이 이미 감지되면 멈춤) 조회시각을 하나로 합쳐두면
    # 실시간 쪼만 계속 돌아도 "정산 확인도 아직 돈다"처럼 보여서 헷갈린다 - 둘로 나눈다.
    runtime.citywaste_timestamp_sensor = VnrisaTimestampSensor(
        entry, "음쓰 조회시각 (citywaste)", "citywaste_timestamp"
    )
    runtime.citywaste_settlement_timestamp_sensor = VnrisaTimestampSensor(
        entry, "음쓰 정산 조회시각 (citywaste)", "citywaste_settlement_timestamp"
    )
    async_add_entities(
        [
            runtime.bill_timestamp_sensor,
            runtime.gas_timestamp_sensor,
            runtime.citywaste_timestamp_sensor,
            runtime.citywaste_settlement_timestamp_sensor,
        ]
    )

    await async_setup_kocom_entry(hass, entry, async_add_entities)


def flatten_bill_items(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """파싱된 고지서 dict를 {key: {display_name, value, unit, attrs}}로 평탄화.

    key는 "전기료 소계"처럼 출처 태그 없는 안정적인 이름(unique_id용), display_name은
    "전기료 소계 (vnrisa)"처럼 화면에 보이는 이름. usage/meters는 charges와 key가
    겹칠 수 있어(예: "전기") 접미사를 붙인다.
    """
    items: dict[str, dict[str, Any]] = {}

    def add(key: str, value: Any, unit: str | None, attrs: dict[str, Any]) -> None:
        items[key] = {"display_name": f"{key} (vnrisa)", "value": value, "unit": unit, "attrs": attrs}

    for key, value in {**data.get("summary", {}), **data.get("details", {})}.items():
        add(key, value, "원", {})

    for key, c in data.get("charges", {}).items():
        add(
            key,
            c.get("value"),
            "원",
            {
                "전월": c.get("prev"),
                "전월차액": c.get("prev_diff"),
                "전년동월": c.get("yoy"),
                "전년동월차액": c.get("yoy_diff"),
                "구분": c.get("group"),
            },
        )

    for name, u in data.get("usage", {}).items():
        add(f"{name} 사용량", u.get("value"), u.get("unit"), {})

    for name, m in data.get("meters", {}).items():
        # ⊙전기/⊙냉수/⊙온수/⊙급탕 헤더 - 전기만 kWh, 나머지(물 계량기)는 전부 "톤"
        # 단위라 m³로 표시(사용량 센서의 _UNIT_MAP과 동일한 환산).
        unit = "kWh" if name == "전기" else "m³"
        add(f"{name} 계량기 지침", m.get("당월"), unit, {"전월": m.get("전월")})

    return items


def _icon_for_key(key: str) -> str:
    """vnrisa 고지서 항목은 이름 자체가 하드코딩 없이 그때그때 생겨서(맨 위 docstring
    참고) 항목별로 아이콘을 일일이 지정할 수 없다 - 그래서 이름 패턴만 보고 고른다.
    같은 패턴이 GSE/citywaste의 고정 키들에도 그대로 들어맞아서 공용으로 쓴다."""
    if key.endswith("사용량"):
        if key.startswith("전기"):
            return "mdi:flash"
        if key.startswith("가스"):
            return "mdi:fire"
        return "mdi:water"  # 수도/냉수/온수/급탕 등
    if key.endswith("계량기 지침"):
        return "mdi:counter"
    if key.endswith("배출량"):
        return "mdi:scale"
    if key.endswith("배출건수"):
        return "mdi:counter"
    if "단가" in key:
        return "mdi:tag-outline"
    return "mdi:currency-krw"  # 그 외 요금/정산금액 성격 항목


_GAS_KEYS = {"가스 사용량", "가스 청구금액", "가스 납부금액", "가스 계량기 지침"}
# restore_orphaned_sensors가 citywaste 항목을 복구할 때 쓴다 - 표시명에서 "(citywaste)"
# 태그만 떼서 vnrisa/GSE처럼 key로 되돌리는 방식은 안 통한다(sync_citywaste_sensors가
# 쓰는 내부 key, 예: "음식물쓰레기 정산금액"이 표시명, 예: "음쓰 정산금액 (citywaste)"과
# 다름) - 그래서 slugify된 안정적인 unique_id 접미사에서 바로 내부 key로 매핑한다.
_CITYWASTE_UID_SUFFIX_TO_KEY = {
    f"_{slugify(k)}": k
    for k in (
        "음식물쓰레기 정산금액",
        "음식물쓰레기 배출량",
        "음식물쓰레기 배출건수",
        "음쓰 kg당 단가",
        "음식물쓰레기 최근배출량",
    )
}
# restore_orphaned_sensors가 복구용 껍데기를 만들 때 쓰는 표시명 - sync_citywaste_sensors가
# 실제로 쓰는 display_name과 반드시 같아야 한다(set_value는 이름을 다시 쓰지 않으므로,
# 여기서 다르면 복구된 엔티티의 이름이 영원히 어긋난 채로 남는다). sync_citywaste_sensors를
# 고치면 이것도 같이 고쳐야 한다.
_CITYWASTE_DISPLAY_NAMES = {
    "음식물쓰레기 정산금액": "음쓰 정산금액 (citywaste)",
    "음식물쓰레기 배출량": "음쓰 총 배출량 (citywaste)",
    "음식물쓰레기 배출건수": "음쓰 배출건수 (citywaste)",
    "음쓰 kg당 단가": "음쓰 단가 (citywaste)",
    "음식물쓰레기 최근배출량": "음쓰 최근 배출량 (citywaste)",
}
_USAGE_KEYS = {"전기 사용량", "수도 사용량", "가스 사용량"}
_TAG_RE = re.compile(r"\s*\((?:vnrisa|GSE)\)$")


def _month_offset(month: str, delta_months: int) -> str:
    """'YYYY-MM' 문자열에서 delta_months만큼 이동한 'YYYY-MM'을 돌려준다(음수=과거) -
    kocom_sensor.py에도 같은 함수가 있다(공용 모듈 없이 각자 둠)."""
    year, mon = int(month[:4]), int(month[5:7])
    total = year * 12 + (mon - 1) + delta_months
    y, m = divmod(total, 12)
    return f"{y:04d}-{m + 1:02d}"


def _prune_history(history: dict[str, Any], this_month: str, keep_months: int = 12) -> None:
    """"작년 같은 달"까지만 보면 되니(지금이 10월이면 작년 10월까지), 12개월이면 충분해서
    그보다 오래된 기록은 계속 쌓아두지 않고 지운다 - kocom_sensor.py에도 같은 함수가
    있다."""
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


async def restore_orphaned_sensors(hass: HomeAssistant, entry: VnrisaConfigEntry) -> None:
    """선거관리운영비처럼 어쩌다 한두 달만 나오는 항목은, 재시작 직후 가장 최근 고지서
    조회에 그 항목이 없으면 이번 세션엔 엔티티 자체가 안 만들어져 "unavailable"로
    남고 history 속성도 안 보인다. 그 항목이 예전에 한 번이라도 있었다는 사실은 엔티티
    레지스트리에 영구히 남아있으니(별도 Store 없이도), 거기 등록된 전부를 훑어서 이번
    조회에 없는 건 값 없는 껍데기로 다시 만들어 RestoreEntity가 recorder에 남은 마지막
    값/history를 복원할 기회를 준다.

    kocom(접두사로 구분)은 대상에서 뺀다 - 매번 전체 항목을 다시 채우는 고정
    센서들이라 "고아"가 될 일이 없다.

    citywaste도 매번 전체 항목을 다시 채우는 고정 센서들이라 "고아"가 될 일은 없지만
    (선거관리운영비 같은 간헐적 항목이 없음), 가스/vnrisa와 달리 음쓰 조회
    (async_refresh_citywaste)가 재시작 중 실패하거나 끝나기 전이면 이번 세션에
    엔티티가 아예 안 만들어져 "사용 불가"로 남는다 - 실제로 이렇게 실패한 사례를
    확인한 건 아니지만(2026-11-06 재시작 직후의 "2026-10":0은 그 달이 아직 정산
    전이라 원래 0이 맞게 찍힌 정상 동작이었다), 가스/vnrisa는 이미 이 안전망이 있는데
    citywaste만 없는 비대칭이라 똑같이 막아둔다. 다만 표시명에서 "(citywaste)" 태그만
    떼는 식(vnrisa/GSE 전용 `_TAG_RE`)으로는 내부 key를 못 찾아서(표시명이 내부
    key와 전혀 다름, 예: "음쓰 정산금액" vs "음식물쓰레기 정산금액")
    `_CITYWASTE_UID_SUFFIX_TO_KEY`로 unique_id 접미사에서 직접 매핑한다."""
    runtime = entry.runtime_data
    registry = er.async_get(hass)
    kocom_prefix = f"{entry.entry_id}_kocom_"
    skip_suffixes = (
        "_bill_timestamp",
        "_gas_timestamp",
        "_citywaste_timestamp",
        "_citywaste_settlement_timestamp",
    )
    new_entities = []
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg_entry.domain != "sensor" or reg_entry.platform != DOMAIN:
            continue
        uid = reg_entry.unique_id
        if uid.startswith(kocom_prefix) or uid.endswith(skip_suffixes):
            continue

        citywaste_key = next((k for suffix, k in _CITYWASTE_UID_SUFFIX_TO_KEY.items() if uid.endswith(suffix)), None)
        if citywaste_key is not None:
            if citywaste_key in runtime.citywaste_sensors:
                continue
            category = None if citywaste_key in _CITYWASTE_PROMINENT_KEYS else EntityCategory.DIAGNOSTIC
            stub_item = {
                "display_name": _CITYWASTE_DISPLAY_NAMES[citywaste_key],
                "value": None,
                "unit": None,
                "attrs": {},
            }
            sensor = VnrisaItemSensor(entry, citywaste_key, stub_item, None, entity_category=category)
            sensor._attr_unique_id = uid
            runtime.citywaste_sensors[citywaste_key] = sensor
            new_entities.append(sensor)
            continue

        key = _TAG_RE.sub("", reg_entry.original_name or uid)
        is_gas = key in _GAS_KEYS
        sensors = runtime.gas_sensors if is_gas else runtime.item_sensors
        if key in sensors:
            continue
        tag = "(GSE)" if is_gas else "(vnrisa)"
        stub_item = {"display_name": f"{key} {tag}", "value": None, "unit": None, "attrs": {}}
        sensor = VnrisaItemSensor(entry, key, stub_item, None)
        sensor._attr_unique_id = uid  # slugify(key)가 등록된 값과 다를 수 있어 그대로 맞춰씀
        sensors[key] = sensor
        new_entities.append(sensor)
    if new_entities and runtime.add_sensor_entities is not None:
        runtime.add_sensor_entities(new_entities)


def sync_item_sensors(entry: VnrisaConfigEntry, data: dict[str, Any]) -> None:
    """고지서 데이터를 받아서, 없는 항목은 새 센서로 추가하고 있는 항목은 값을 갱신(또는
    과거 달이면 history에만 추가)한다."""
    runtime = entry.runtime_data
    month = data.get("month")
    new_entities = []
    for key, item in flatten_bill_items(data).items():
        existing = runtime.item_sensors.get(key)
        if existing is None:
            sensor = VnrisaItemSensor(entry, key, item, month)
            runtime.item_sensors[key] = sensor
            new_entities.append(sensor)
        else:
            existing.set_value(item, month)
    if new_entities and runtime.add_sensor_entities is not None:
        runtime.add_sensor_entities(new_entities)


def sync_gas_sensors(entry: VnrisaConfigEntry, rows: list[dict[str, Any]]) -> None:
    """도시가스 로그인 조회 결과(gas.py의 fetch_gas_history - 여러 달치 한번에)를
    가스 사용량/청구금액/납부금액/계량기 지침 센서에 반영. 각 달이 VnrisaItemSensor에
    그대로 들어가므로(이미 history 병합 로직이 있음) 월 순서와 무관하게 안전하게
    누적된다.

    관리비 고지서(item_sensors)와 별도의 dict(gas_sensors)에서 관리 - 출처가 다른
    데이터를 섞지 않기 위해 분리.
    """
    runtime = entry.runtime_data
    new_entities = []
    for row in rows:
        month = row.get("month")
        items = {
            "가스 사용량": {
                "display_name": "가스 사용량 (GSE)",
                "value": row.get("usage_m3"),
                "unit": "㎥",
                "attrs": {"검침코드": row.get("meter_status")},
            },
            "가스 청구금액": {
                "display_name": "가스 청구금액 (GSE)",
                "value": row.get("billed_amount"),
                "unit": "원",
                "attrs": {},
            },
            "가스 납부금액": {
                "display_name": "가스 납부금액 (GSE)",
                "value": row.get("paid_amount"),
                "unit": "원",
                "attrs": {"수납일": row.get("payment_date"), "미납": row.get("unpaid")},
            },
            "가스 계량기 지침": {
                "display_name": "가스 계량기 지침 (GSE)",
                "value": row.get("meter_reading"),
                "unit": "㎥",
                "attrs": {},
            },
        }
        for key, item in items.items():
            existing = runtime.gas_sensors.get(key)
            if existing is None:
                sensor = VnrisaItemSensor(entry, key, item, month)
                runtime.gas_sensors[key] = sensor
                new_entities.append(sensor)
            else:
                existing.set_value(item, month)
    if new_entities and runtime.add_sensor_entities is not None:
        runtime.add_sensor_entities(new_entities)


_CITYWASTE_PROMINENT_KEYS = {"음식물쓰레기 배출량", "음식물쓰레기 배출건수", "음식물쓰레기 최근배출량"}
# "최근배출량"은 "이번 달 총량"이 아니라 "가장 최근 한 건의 kg"이라 지난 달 값이라는
# 개념 자체가 없다(그 달이 끝나면 의미가 없어짐) - 다른 키들처럼 12개월 쌓아두지 않고
# 이번 달 것만 남긴다.
_CITYWASTE_CURRENT_MONTH_ONLY_KEYS = {"음식물쓰레기 최근배출량"}
# 정산금액/배출량/배출건수는 매달 history에 쌓이기만 하고 가스/사용량(_GAS_KEYS/
# _USAGE_KEYS)처럼 가지치기 대상이 아니었다 - 그대로 두면 설치 기간이 길어질수록
# attribute가 끝없이 커진다. 똑같이 12개월(작년 같은 달 비교에 필요한 만큼)만 남긴다.
_CITYWASTE_PRUNED_KEYS = {"음식물쓰레기 정산금액", "음식물쓰레기 배출량", "음식물쓰레기 배출건수"}
# 이번 달 행의 배출량/배출건수/최근배출량은 citywaste.py의 실시간 API 호출이
# 실패하면(네트워크 오류 등) 그 자체로 아예 안 들어있다 - row.get(...)이 그냥 None을
# 돌려주는데, 이걸 "정말 0건/0kg"으로 착각해 덮어쓰면 안 되니 live_ok=False인 행에서는
# 이 셋도 정산금액의 pending과 같은 방식으로 헤드라인 값을 보존한다.
_CITYWASTE_LIVE_KEYS = {"음식물쓰레기 배출량", "음식물쓰레기 배출건수", "음식물쓰레기 최근배출량"}


def sync_citywaste_sensors(
    entry: VnrisaConfigEntry, rows: list[dict[str, Any]], unit_price: float | int | None = None
) -> None:
    """citywaste.py의 fetch_citywaste_history(음쓰 RFID 종량기 - 여러 달치) 결과를
    정산금액/배출량/배출건수 센서에 반영. gas_sensors와 같은 패턴, 별도 dict로 관리.

    표시명은 "음쓰"로 보이지만 dict의 key(=unique_id 재료)는 예전 그대로 둔다 -
    여기서 key를 바꾸면 slugify 결과가 달라져서 entity_id가 바뀌고 history가 끊긴다
    (flatten_bill_items의 key/display_name 분리 원칙과 동일).

    kg당 단가는 달 구분 없는 고정값이라 month 없이(=항상 최신으로 취급) 반영한다.

    정산금액(final_amount)은 citywaste.py가 이번 달 행을 항상 "아직 정산 전"이라는
    뜻으로 0을 끼워 보낸다(row.get("pending")) - 그 행이 헤드라인 값을 덮어쓰지
    않도록 VnrisaItemSensor.set_value에 pending을 그대로 넘긴다. 배출량/배출건수/
    최근배출량은 보통 이번 달 것도 실시간 API로 받은 진짜 값이라 pending을 안 주지만,
    그 실시간 API 호출 자체가 실패한 행(row.get("live_ok") is False)에서는 이 셋도
    값이 통째로 비어 있으므로(0건/0kg이 아니라 "이번엔 못 받아옴") 똑같이 pending
    처리해서 헤드라인의 마지막 성공값을 보존한다."""
    runtime = entry.runtime_data
    new_entities = []
    for row in rows:
        month = row.get("month")
        settlement_pending = bool(row.get("pending"))
        live_pending = not row.get("live_ok", True)
        items = {
            "음식물쓰레기 정산금액": {
                "display_name": "음쓰 정산금액 (citywaste)",
                "value": row.get("final_amount"),
                "unit": "원",
                "attrs": {},
            },
            "음식물쓰레기 배출량": {
                "display_name": "음쓰 총 배출량 (citywaste)",
                "value": row.get("discharge_kg"),
                "unit": "kg",
                "attrs": {},
            },
            "음식물쓰레기 배출건수": {
                "display_name": "음쓰 배출건수 (citywaste)",
                "value": row.get("discharge_count"),
                "unit": "회",
                "attrs": {},
            },
            "음식물쓰레기 최근배출량": {
                "display_name": "음쓰 최근 배출량 (citywaste)",
                "value": row.get("last_discharge_kg"),
                "unit": "kg",
                "attrs": {},
            },
        }
        for key, item in items.items():
            if key == "음식물쓰레기 정산금액":
                pending = settlement_pending
            elif key in _CITYWASTE_LIVE_KEYS:
                pending = live_pending
            else:
                pending = False
            existing = runtime.citywaste_sensors.get(key)
            if existing is None:
                category = None if key in _CITYWASTE_PROMINENT_KEYS else EntityCategory.DIAGNOSTIC
                sensor = VnrisaItemSensor(entry, key, item, month, entity_category=category, pending=pending)
                runtime.citywaste_sensors[key] = sensor
                new_entities.append(sensor)
            else:
                existing.set_value(item, month, pending=pending)

    if unit_price is not None:
        key = "음쓰 kg당 단가"
        item = {"display_name": "음쓰 단가 (citywaste)", "value": unit_price, "unit": "원/kg", "attrs": {}}
        existing = runtime.citywaste_sensors.get(key)
        if existing is None:
            sensor = VnrisaItemSensor(entry, key, item, None)
            runtime.citywaste_sensors[key] = sensor
            new_entities.append(sensor)
        else:
            existing.set_value(item, None)

    if new_entities and runtime.add_sensor_entities is not None:
        runtime.add_sensor_entities(new_entities)


class VnrisaTimestampSensor(RestoreEntity, SensorEntity):
    """코콤의 '코콤 조회시각'처럼, vnrisa 관리비/GSE 가스도 마지막으로 성공한
    조회시각을 볼 수 있게 - 조회가 조용히 하루 종일 실패하고 있어도 바로 알아챌 수
    있다."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:clock-check-outline"

    def __init__(self, entry: VnrisaConfigEntry, name: str, unique_suffix: str) -> None:
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{unique_suffix}"
        self._attr_device_info = _device_info(entry)
        self._attr_native_value = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in (None, "unknown", "unavailable"):
            self._attr_native_value = last_state.state

    def set_value(self, value: str) -> None:
        self._attr_native_value = value
        if self.hass is not None:
            self.async_write_ha_state()


class VnrisaItemSensor(RestoreEntity, SensorEntity):
    """vnrisa 관리비/GSE 가스 출처 센서는 전부 진단 카테고리 - 상단에 바로 보이는 건
    코콤의 전기/가스/수도 사용량 3개뿐이다(kocom_sensor.py, 이 클래스를 안 씀).

    구성(CONFIG)으로 옮겨봤다가 전부 "사용할 수 없음"이 됐었다 - HA가 SensorEntity는
    구성 카테고리로 추가하는 것 자체를 거부한다(homeassistant/components/sensor/
    __init__.py에서 하드코딩된 검증, "cannot be added as the entity category is set
    to config"). 구성은 switch/number/select/text처럼 사용자가 직접 값을 바꾸는
    엔티티 전용이라 읽기 전용 센서에는 애초에 쓸 수 없다."""

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: VnrisaConfigEntry,
        key: str,
        item: dict[str, Any],
        month: str | None,
        entity_category: EntityCategory | None = EntityCategory.DIAGNOSTIC,
        pending: bool = False,
    ) -> None:
        self._key = key
        self._attr_entity_category = entity_category
        self._attr_name = item.get("display_name", key)
        self._attr_unique_id = f"{entry.entry_id}_{slugify(key)}"
        self._attr_icon = _icon_for_key(key)
        self._attr_device_info = _device_info(entry)
        self._attr_native_unit_of_measurement = item.get("unit")
        self._latest_month: str | None = None
        self._history: dict[str, Any] = {}
        self._regular_attrs: dict[str, Any] = {}
        self.set_value(item, month, pending=pending)

    def _build_extra_attrs(self) -> dict[str, Any]:
        attrs = dict(self._regular_attrs)
        if self._latest_month:
            if self._key in _USAGE_KEYS:
                last_m = _month_offset(self._latest_month, -1)
                if last_m in self._history:
                    attrs["전월"] = self._history[last_m]
                year_ago = _nearest_year_ago(self._history, self._latest_month)
                if year_ago is not None:
                    attrs["작년"] = year_ago
            # 가스 쪽은 _USAGE_KEYS(사용량)만이 아니라 청구금액/납부금액/계량기 지침도
            # 12개월로 맞춘다 - 안 그러면 매번 fetch_gas_history가 다시 가져오는
            # 24개월(months_back 기본값)치가 그대로 다 쌓여서 사용량만 12개월, 나머지
            # 3개는 24개월로 서로 안 맞았다.
            if self._key in _USAGE_KEYS or self._key in _GAS_KEYS or self._key in _CITYWASTE_PRUNED_KEYS:
                _prune_history(self._history, self._latest_month)
            elif self._key in _CITYWASTE_CURRENT_MONTH_ONLY_KEYS:
                for month in [m for m in self._history if m != self._latest_month]:
                    del self._history[month]
        attrs["history"] = dict(sorted(self._history.items()))
        return attrs

    async def async_added_to_hass(self) -> None:
        """재시작 시 __init__.py가 가장 최근 링크 하나만 자동 재조회해서 '이번 세션에
        이미 값이 있다'고 착각하고 과거 history를 복원 안 하면, 그 전 재시작들 때 쌓인
        다른 달 기록이 매번 날아간다 - 그래서 이미 값이 있어도 history는 항상 병합하고,
        현재 표시값만 "이번 세션에 실제로 받은 값이 있으면 그걸 우선"한다."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (None, "unknown", "unavailable"):
            return

        old_attrs = dict(last_state.attributes)
        old_history = dict(old_attrs.pop("history", {}) or {})
        for month, value in old_history.items():
            self._history.setdefault(month, value)

        if self._latest_month is None:
            # 이번 세션에 아직 실제 조회가 없었음 - 복원된 값을 그대로 현재값으로 사용
            value = last_state.state
            try:
                value = float(value) if "." in value else int(value)
            except ValueError:
                pass
            self._attr_native_value = value
            self._regular_attrs = old_attrs
            self._latest_month = max(self._history) if self._history else None

        self._attr_extra_state_attributes = self._build_extra_attrs()
        if self.hass is not None:
            self.async_write_ha_state()

    def set_value(self, item: dict[str, Any], month: str | None, pending: bool = False) -> None:
        """`pending=True`는 "이번 달은 아직 정산 전이라 0으로 찍힌 자리 채우기용 값"
        이라는 뜻 - citywaste.py의 정산금액이 매번 이번 달을 final_amount=0으로
        끼워 보내는데, 그게 월 비교(`month >= self._latest_month`)에서 항상 이겨서
        헤드라인 값을 계속 0으로 덮어쓰는 문제가 있었다(실제 확정된 지난달 값은
        history에는 멀쩡히 남지만 화면 메인 값만 0으로 보임). pending인 행은
        history에는 그대로 기록하되(그래야 "이번 달 아직 0"이라는 정보 자체는
        남는다) `_latest_month`/헤드라인 값은 건드리지 않아서, 메인 값이 계속
        마지막으로 확정된 달 것을 보여주게 한다."""
        if month is not None:
            self._history[month] = item.get("value")
            is_latest = not pending and (self._latest_month is None or month >= self._latest_month)
        else:
            is_latest = True  # 월 정보가 없으면(이례적) 그냥 최신으로 취급

        if is_latest:
            self._latest_month = month
            self._attr_native_value = item.get("value")
            self._regular_attrs = {k: v for k, v in item.get("attrs", {}).items() if v is not None}

        self._attr_extra_state_attributes = self._build_extra_attrs()
        if self.hass is not None:
            self.async_write_ha_state()
