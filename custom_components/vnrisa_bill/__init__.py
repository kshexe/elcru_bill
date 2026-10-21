"""엘크루 관리비 - 관리비(vnrisa) + 도시가스(GSE) + 코콤 에너지 통합.

관리비 고지서는 텍스트 입력창에 그 달 링크를 붙여넣으면 즉시 조회(Token이 매달
바뀌어서 자동 재조회가 불가능 - 실측 확인됨). 다만 "링크를 넣을 때만 조회"라고 해서
재시작할 때 센서가 다시 비는 건 별개 문제였다 - 마지막으로 성공한 링크를 Store에
저장해뒀다가, 설치/재시작 직후 그 링크로 자동으로 한 번 더 조회해서 센서를 채운다
(entry.data에 넣으면 옵션 변경 감지용 update_listener가 매번 전체 reload를 일으켜서,
그보다 가벼운 Store를 쓴다).

도시가스는 설정(최초 설치 시)에서 입력한 아이디/비밀번호로 매일 지정 시각(설정 →
옵션에서 변경 가능, 기본 09:00)에 로그인해서 24개월치 고지/수납내역을 한 번에
재조회한다 - 로그인도 매번 새로 하므로(세션을 유지하지 않음) 세션 만료를 신경 쓸
필요가 없다.
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
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store

from .const import (
    CONF_CITYWASTE_ID,
    CONF_CITYWASTE_PWD,
    CONF_GAS_CHECK_TIME,
    CONF_GAS_MEMBER_ID,
    CONF_GAS_MEMBER_PWD,
    DEFAULT_CITYWASTE_CHECK_TIME,
    DEFAULT_GAS_CHECK_TIME,
    DOMAIN,
    PLATFORMS,
)
from .citywaste import CitywasteLoginError, fetch_citywaste_history
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

    check_time = entry.options.get(CONF_GAS_CHECK_TIME, DEFAULT_GAS_CHECK_TIME)
    hour, minute = (int(x) for x in check_time.split(":"))

    async def _gas_scheduled(_now) -> None:
        await async_refresh_gas(hass, entry)

    entry.async_on_unload(
        async_track_time_change(hass, _gas_scheduled, hour=hour, minute=minute, second=0)
    )

    citywaste_hour, citywaste_minute = (int(x) for x in DEFAULT_CITYWASTE_CHECK_TIME.split(":"))

    async def _citywaste_scheduled(now) -> None:
        # citywaste는 한 번 완료된 달 정산내역이 그 뒤로 안 바뀌어서 매일 다시 긁을
        # 이유가 없다 - 매달 1일에만 확인한다.
        if now.day == 1:
            await async_refresh_citywaste(hass, entry)

    entry.async_on_unload(
        async_track_time_change(
            hass, _citywaste_scheduled, hour=citywaste_hour, minute=citywaste_minute, second=0
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
        rows, unit_price = await fetch_citywaste_history(member_id, member_pwd, months_back=months_back)
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
