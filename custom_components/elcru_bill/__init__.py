"""엘크루 관리비 - 관리비(vnrisa) + 도시가스(GSE) + 코콤 통합.

관리비 고지서는 텍스트 입력창에 그 달 링크를 붙여넣으면 즉시 조회(Token이 매달
바뀌어서 자동 재조회가 불가능 - 실측 확인됨). 다만 "링크를 넣을 때만 조회"라고 해서
재시작할 때 센서가 다시 비는 건 별개 문제였다 - 마지막으로 성공한 링크를 Store에
저장해뒀다가, 설치/재시작 직후 그 링크로 자동으로 한 번 더 조회해서 센서를 채운다
(entry.data에 넣으면 옵션 변경 감지용 update_listener가 매번 전체 reload를 일으켜서,
그보다 가벼운 Store를 쓴다).

도시가스/음쓰는 설정에서 입력한 아이디/비밀번호로 매시간 재조회하다가, "최신월"(지난달
데이터)이 실제로 바뀐 걸 감지하면 그 즉시 알림을 보내고 그 달은 더 조회하지 않는다 -
고정된 확인시각 대신 이 방식을 쓰는 이유는 GSE/citywaste 둘 다 지난달 데이터가 몇 시에
올라올지, 심지어 당일에 올라오는지조차 미리 알 수 없기 때문이다(실측 결과 1일 하루
종일 안 바뀐 적도 있음). 로그인도 매번 새로 하므로(세션을 유지하지 않음) 세션 만료를
신경 쓸 필요가 없다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import datetime
import logging
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_change, async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import (
    CONF_CITYWASTE_DONG,
    CONF_CITYWASTE_HO,
    CONF_CITYWASTE_ID,
    CONF_CITYWASTE_PWD,
    CONF_CITYWASTE_REFRESH_INTERVAL,
    CONF_CITYWASTE_TAG_ID,
    CONF_GAS_MEMBER_ID,
    CONF_GAS_MEMBER_PWD,
    DEFAULT_CITYWASTE_REFRESH_INTERVAL,
    DOMAIN,
    PLATFORMS,
)
from .citywaste import CitywasteLoginError, fetch_citywaste_history, fetch_citywaste_live
from .gas import GasLoginError, fetch_gas_history

_LOGGER = logging.getLogger(__name__)

type VnrisaConfigEntry = ConfigEntry[VnrisaRuntimeData]


@dataclass
class VnrisaRuntimeData:
    """text.py/sensor.py가 공유하는 상태.

    parkingweb/myhome_parking과 같은 패턴 - 엔티티가 자기 자신을 여기 등록해두고,
    다른 엔티티가 직접 참조로 상태를 갱신한다(디스패처 신호 없이).
    """

    # 관리비 고지서 (링크를 붙여넣을 때만 조회, 재시작 시엔 url_store에 저장된 마지막
    # 링크로 자동 재조회)
    url: str = ""
    url_store: Store | None = None
    data: dict[str, Any] | None = None
    error: str | None = None
    text_entity: Any = None
    item_sensors: dict[str, Any] = field(default_factory=dict)
    add_sensor_entities: Any = None
    bill_timestamp_sensor: Any = None

    # 도시가스 (아이디/비밀번호는 entry.data에 저장 - 여기엔 조회 결과만)
    gas_error: str | None = None
    gas_sensors: dict[str, Any] = field(default_factory=dict)
    gas_timestamp_sensor: Any = None

    # 음쓰(citywaste) - 아이디/비밀번호는 entry.data에 저장. backfill_store는
    # "과거 1년치를 이미 한 번 다 긁었는지"만 기억해서, 매번 12달치를 다시 조회하지
    # 않고 그 뒤로는 최근 2달만 가볍게 갱신한다.
    citywaste_error: str | None = None
    citywaste_sensors: dict[str, Any] = field(default_factory=dict)
    citywaste_timestamp_sensor: Any = None
    citywaste_backfill_store: Store | None = None
    # 실시간 전용 폴링(async_refresh_citywaste_live)이 매번 로그인하지 않고 바로 쓸 수
    # 있게, 월 1회 도는 전체 조회(async_refresh_citywaste) 때마다 갱신해둔다.
    citywaste_tag_id: str | None = None
    citywaste_dong: str | None = None
    citywaste_ho: str | None = None


async def async_setup_entry(hass: HomeAssistant, entry: VnrisaConfigEntry) -> bool:
    runtime = VnrisaRuntimeData()
    runtime.url_store = Store(hass, 1, f"{DOMAIN}_{entry.entry_id}_bill_url")
    runtime.citywaste_backfill_store = Store(hass, 1, f"{DOMAIN}_{entry.entry_id}_citywaste_backfill")
    entry.runtime_data = runtime

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    stored_url = await runtime.url_store.async_load()
    if stored_url:
        await async_refresh_bill(hass, entry, stored_url)

    await async_refresh_gas(hass, entry)  # 설치/재시작 직후 바로 한 번 조회
    await async_refresh_citywaste(hass, entry)  # 설치/재시작 직후 바로 한 번 조회(1회성 백필 포함)

    from .sensor import restore_orphaned_sensors  # 순환 임포트 방지용 지연 임포트

    # 이번 세션 조회 결과엔 없지만(예: 선거관리운영비처럼 특정 달에만 나오는 항목)
    # 엔티티 레지스트리에 전에 등록된 적 있는 항목은 값 없는 껍데기로 다시 만들어,
    # recorder에 남은 마지막 값/history를 복원할 기회를 준다.
    await restore_orphaned_sensors(hass, entry)

    # 가스/음쓰 둘 다 "지난달" 데이터가 정확히 몇 시에 올라올지 미리 알 수 없어서(실측
    # 결과 당일엔 안 올라오고 며칠씩 걸림), 고정 시각에 하루 한 번 확인하는 대신 매시간
    # 확인하다가 "최신월"이 실제로 바뀌면 그 즉시 알림을 보내고 그 달은 더 조회하지
    # 않는다(_gas_detected_month/_citywaste_detected_month) - 다음 달 1일이 되면
    # 자동으로 다시 매시간 확인을 시작한다. 변경 전까지의 매시간 호출은 그냥
    # async_refresh_gas/async_refresh_citywaste를 다시 부르는 것뿐이라, 이미 최신인
    # 달엔 실질적으로 하는 일이 없다(API만 한 번씩 더 치는 정도).
    _gas_last_month = {"value": None}
    _gas_detected_month = {"value": None}

    async def _gas_scheduled(now) -> None:
        this_cycle = now.strftime("%Y-%m")
        if _gas_detected_month["value"] == this_cycle:
            return
        await async_refresh_gas(hass, entry)
        usage_sensor = entry.runtime_data.gas_sensors.get("가스 사용량")
        latest_month = getattr(usage_sensor, "_latest_month", None)
        usage_value = getattr(usage_sensor, "_attr_native_value", None)
        _LOGGER.debug(
            "[가스 확인] %s - 최신월=%s, 사용량=%s",
            now.strftime("%Y-%m-%d %H:%M:%S"),
            latest_month,
            usage_value,
        )
        previous = _gas_last_month["value"]
        if previous is not None and latest_month is not None and latest_month != previous:
            _LOGGER.info("[가스 반영] 최신월이 %s -> %s로 바뀜, 사용량=%s", previous, latest_month, usage_value)
            await hass.services.async_call(
                "notify",
                "mobile_app_gangseongho",
                {
                    "title": "가스 반영",
                    "message": f"최신월이 {previous} -> {latest_month}로 바뀜 ({now.strftime('%Y-%m-%d %H:%M')}, 사용량={usage_value})",
                },
            )
            _gas_detected_month["value"] = this_cycle
        _gas_last_month["value"] = latest_month

    entry.async_on_unload(
        async_track_time_change(hass, _gas_scheduled, minute=0, second=0)
    )

    _citywaste_last_month = {"value": None}
    _citywaste_detected_month = {"value": None}

    async def _citywaste_scheduled(now) -> None:
        this_cycle = now.strftime("%Y-%m")
        if _citywaste_detected_month["value"] == this_cycle:
            return
        await async_refresh_citywaste(hass, entry)
        settle_sensor = entry.runtime_data.citywaste_sensors.get("음식물쓰레기 정산금액")
        latest_month = getattr(settle_sensor, "_latest_month", None)
        settle_value = getattr(settle_sensor, "_attr_native_value", None)
        _LOGGER.debug(
            "[음쓰 확인] %s - 최신월=%s, 정산금액=%s",
            now.strftime("%Y-%m-%d %H:%M:%S"),
            latest_month,
            settle_value,
        )
        previous = _citywaste_last_month["value"]
        if previous is not None and latest_month is not None and latest_month != previous:
            _LOGGER.info("[음쓰 반영] 최신월이 %s -> %s로 바뀜, 정산금액=%s", previous, latest_month, settle_value)
            await hass.services.async_call(
                "notify",
                "mobile_app_gangseongho",
                {
                    "title": "음쓰 반영",
                    "message": f"최신월이 {previous} -> {latest_month}로 바뀜 ({now.strftime('%Y-%m-%d %H:%M')}, 정산금액={settle_value})",
                },
            )
            _citywaste_detected_month["value"] = this_cycle
        _citywaste_last_month["value"] = latest_month

    entry.async_on_unload(
        async_track_time_change(hass, _citywaste_scheduled, minute=0, second=0)
    )

    citywaste_refresh_interval = entry.data.get(
        CONF_CITYWASTE_REFRESH_INTERVAL, DEFAULT_CITYWASTE_REFRESH_INTERVAL
    )

    async def _citywaste_live_scheduled(_now) -> None:
        await async_refresh_citywaste_live(hass, entry)

    if citywaste_refresh_interval:
        # 0 = 꺼짐(코콤의 CONF_KOCOM_UPDATE_INTERVAL과 같은 규칙) - timedelta(seconds=0)로
        # 등록하면 간격 없이 계속 도는 타이머가 돼버린다.
        entry.async_on_unload(
            async_track_time_interval(
                hass, _citywaste_live_scheduled, datetime.timedelta(seconds=citywaste_refresh_interval)
            )
        )
    entry.async_on_unload(entry.add_update_listener(_reload))

    return True


async def _reload(hass: HomeAssistant, entry: VnrisaConfigEntry) -> None:
    """옵션(가스 조회 시각)이 바뀌면 다시 로드해서 새 시각으로 예약을 다시 건다."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: VnrisaConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_refresh_bill(hass: HomeAssistant, entry: VnrisaConfigEntry, url: str) -> bool:
    """고지서 링크를 조회해서 센서에 반영하고, 성공하면 Store에 저장해 다음 재시작 때도
    자동으로 다시 쓸 수 있게 한다. text.py(사용자가 직접 입력)와 여기(재시작 시 자동
    재조회) 양쪽에서 같이 쓰는 공용 함수."""
    from .fetch import fetch_bill  # 순환 임포트 방지용 지연 임포트
    from .sensor import sync_item_sensors

    runtime = entry.runtime_data
    runtime.url = url
    issue_id = f"{entry.entry_id}_bill_fetch_failed"
    try:
        data = await fetch_bill(url)
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.warning("고지서 조회 실패: %s", err)
        runtime.error = f"조회 실패: {err}"
        _create_fetch_issue(hass, issue_id, "bill_fetch_failed", str(err))
        return False

    if not data.get("month") or not data.get("summary"):
        _LOGGER.warning("고지서 항목을 찾지 못함 (페이지 구조 변경 또는 잘못된 링크)")
        runtime.error = "고지서 항목을 찾지 못함 - 링크가 올바른지 확인하세요"
        _create_fetch_issue(hass, issue_id, "bill_fetch_failed", runtime.error)
        return False

    runtime.error = None
    runtime.data = data
    sync_item_sensors(entry, data)
    ir.async_delete_issue(hass, DOMAIN, issue_id)
    if runtime.bill_timestamp_sensor is not None:
        runtime.bill_timestamp_sensor.set_value(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if runtime.url_store is not None:
        await runtime.url_store.async_save(url)
    return True


async def async_refresh_gas(hass: HomeAssistant, entry: VnrisaConfigEntry) -> None:
    """entry.data에 저장된 아이디/비밀번호로 로그인해서 고지/수납내역을 전부 조회해
    센서에 반영한다."""
    from .sensor import sync_gas_sensors  # 순환 임포트 방지용 지연 임포트

    runtime = entry.runtime_data
    issue_id = f"{entry.entry_id}_gas_login_failed"
    member_id = entry.data.get(CONF_GAS_MEMBER_ID)
    member_pwd = entry.data.get(CONF_GAS_MEMBER_PWD)
    if not member_id or not member_pwd:
        return

    try:
        rows = await fetch_gas_history(member_id, member_pwd)
    except GasLoginError as err:
        _LOGGER.warning("도시가스 로그인/조회 실패: %s", err)
        runtime.gas_error = str(err)
        _create_fetch_issue(hass, issue_id, "gas_login_failed", str(err))
        return
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.warning("도시가스 조회 중 통신 오류: %s", err)
        runtime.gas_error = f"조회 실패: {err}"
        _create_fetch_issue(hass, issue_id, "gas_login_failed", str(err))
        return

    runtime.gas_error = None
    sync_gas_sensors(entry, rows)
    ir.async_delete_issue(hass, DOMAIN, issue_id)
    if runtime.gas_timestamp_sensor is not None:
        runtime.gas_timestamp_sensor.set_value(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


async def async_refresh_citywaste(hass: HomeAssistant, entry: VnrisaConfigEntry) -> None:
    """entry.data에 저장된 아이디/비밀번호로 로그인해서 음쓰 정산내역을 조회해
    센서에 반영한다. GSE처럼 한 번에 전체 기간을 안 주고 달마다 따로 요청해야 해서,
    처음 한 번만 1년치를 긁고(backfill_store에 기록) 그 뒤로는 지난 달 1개월만
    가볍게 갱신한다 - 이번 달은(fetch_citywaste_history 안에서) 페이지 요청 없이
    항상 실시간 조회로만 채워지니 months_back에는 안 들어간다."""
    from .sensor import sync_citywaste_sensors  # 순환 임포트 방지용 지연 임포트

    runtime = entry.runtime_data
    issue_id = f"{entry.entry_id}_citywaste_login_failed"
    member_id = entry.data.get(CONF_CITYWASTE_ID)
    member_pwd = entry.data.get(CONF_CITYWASTE_PWD)
    if not member_id or not member_pwd:
        return

    backfilled = False
    if runtime.citywaste_backfill_store is not None:
        backfilled = bool((await runtime.citywaste_backfill_store.async_load() or {}).get("done"))
    months_back = 1 if backfilled else 11

    try:
        rows, unit_price, _tag_id, _dong, _ho = await fetch_citywaste_history(
            member_id, member_pwd, months_back=months_back
        )
    except CitywasteLoginError as err:
        _LOGGER.warning("음쓰(citywaste) 로그인/조회 실패: %s", err)
        runtime.citywaste_error = str(err)
        _create_fetch_issue(hass, issue_id, "citywaste_login_failed", str(err))
        return
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.warning("음쓰(citywaste) 조회 중 통신 오류: %s", err)
        runtime.citywaste_error = f"조회 실패: {err}"
        _create_fetch_issue(hass, issue_id, "citywaste_login_failed", str(err))
        return

    runtime.citywaste_error = None
    sync_citywaste_sensors(entry, rows, unit_price)
    ir.async_delete_issue(hass, DOMAIN, issue_id)
    if not backfilled and runtime.citywaste_backfill_store is not None:
        await runtime.citywaste_backfill_store.async_save({"done": True})
    # 실시간 조회(async_refresh_citywaste_live)는 매번 로그인/정산내역 페이지를 다시
    # 파싱하지 않고, 설정(음쓰 설정 화면)에서 사용자가 직접 입력해 저장한
    # 태그인쇄번호/동/호만 그대로 쓴다 - 자동 추출/채움은 없다.
    tag_id = entry.data.get(CONF_CITYWASTE_TAG_ID)
    dong = entry.data.get(CONF_CITYWASTE_DONG)
    ho = entry.data.get(CONF_CITYWASTE_HO)
    if tag_id and dong and ho:
        runtime.citywaste_tag_id, runtime.citywaste_dong, runtime.citywaste_ho = tag_id, dong, ho
    if runtime.citywaste_timestamp_sensor is not None:
        runtime.citywaste_timestamp_sensor.set_value(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


async def async_refresh_citywaste_live(hass: HomeAssistant, entry: VnrisaConfigEntry) -> None:
    """정산내역(월 1회, async_refresh_citywaste)과 분리된 실시간 전용 폴링 - 로그인이
    필요 없는 API만 불러서 이번 달 배출량/배출건수만 더 자주(기본 1800초, 옵션에서
    변경 가능) 갱신한다. 태그ID/동/호수는 async_refresh_citywaste가 돌 때마다
    runtime에 캐시해둔 걸 그대로 쓰고, 아직 한 번도 안 돌았으면(캐시가 없으면)
    조용히 건너뛴다."""
    from .sensor import sync_citywaste_sensors  # 순환 임포트 방지용 지연 임포트

    runtime = entry.runtime_data
    if not (runtime.citywaste_tag_id and runtime.citywaste_dong and runtime.citywaste_ho):
        return

    try:
        live = await fetch_citywaste_live(runtime.citywaste_tag_id, runtime.citywaste_dong, runtime.citywaste_ho)
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.warning("음쓰(citywaste) 실시간 조회 중 통신 오류: %s", err)
        return
    if live is None:
        return

    this_month = datetime.datetime.now().strftime("%Y-%m")
    # pending=True: citywaste.py의 fetch_citywaste_history와 같은 이유로, 이번 달
    # final_amount=0이 "음쓰 정산금액" 헤드라인 값을 덮어쓰지 않게 한다.
    sync_citywaste_sensors(entry, [{"month": this_month, "final_amount": 0, "pending": True, **live}])
    if runtime.citywaste_timestamp_sensor is not None:
        runtime.citywaste_timestamp_sensor.set_value(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


def _create_fetch_issue(hass: HomeAssistant, issue_id: str, translation_key: str, error: str) -> None:
    """조회 실패를 로그에만 남기면 조용히 계속 실패해도 못 알아채서, 설정 > 수리
    항목으로 바로 눈에 띄게 한다 - 다음 조회가 성공하면 호출한 쪽에서 지운다."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=translation_key,
        translation_placeholders={"error": error},
    )
