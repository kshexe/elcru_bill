"""설정 2단계: ① GSE 도시가스 로그인(아이디/비밀번호, 로그인 검증까지 함) ② 코콤 아이디/
비밀번호로 서버 IP 자동 조회 + 인증까지 한 화면에서 검증 후 전부 저장. 관리비 고지서는
입력값이 없다 - 설치 후 '고지서 링크' 텍스트 엔티티에 붙여넣는 방식 그대로."""
from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp
import async_timeout
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CITYWASTE_ID,
    CONF_CITYWASTE_PWD,
    CONF_CITYWASTE_REFRESH_INTERVAL,
    CONF_GAS_CHECK_TIME,
    CONF_GAS_MEMBER_ID,
    CONF_GAS_MEMBER_PWD,
    CONF_KOCOM_IP,
    CONF_KOCOM_PASSWORD,
    CONF_KOCOM_UPDATE_INTERVAL,
    CONF_KOCOM_USERNAME,
    DEFAULT_CITYWASTE_REFRESH_INTERVAL,
    DEFAULT_GAS_CHECK_TIME,
    DEFAULT_KOCOM_UPDATE_INTERVAL,
    DOMAIN,
)
from .citywaste import CitywasteLoginError, fetch_citywaste_history
from .gas import GasLoginError, fetch_gas_history
from .kocom_api import KocomAPI
from .kocom_exceptions import IpAddressNotFoundError
from .kocom_util import md5_hashing, string_to_padded_hex

_LOGGER = logging.getLogger(__name__)
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")

GAS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_GAS_MEMBER_ID): str,
        vol.Required(CONF_GAS_MEMBER_PWD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)


async def _lookup_kocom_ip(username: str) -> str:
    """코콤 아이디로 단지 월패드 서버 IP를 조회한다(kocom_energy 원본과 동일한 방식) -
    설정 최초 단계와 옵션에서 로그인을 다시 할 때 둘 다 쓴다."""
    async with aiohttp.ClientSession() as session, async_timeout.timeout(10):
        resp = await session.get(f"http://221.141.3.28/SvrInfo.php?uid={username}")
        resp.raise_for_status()
        response_text = await resp.text()
    ip_match = re.search(r"3 => ([\d.]+)", response_text)
    if not ip_match:
        raise IpAddressNotFoundError("IP address not found in the response")
    return ip_match.group(1)


class VnrisaConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._gas_data: dict[str, Any] = {}
        self._kocom_username: str = ""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """① GSE 도시가스 로그인."""
        self._async_abort_entries_match()
        errors: dict[str, str] = {}
        if user_input is not None:
            member_id = user_input[CONF_GAS_MEMBER_ID].strip()
            member_pwd = user_input[CONF_GAS_MEMBER_PWD]
            try:
                await fetch_gas_history(member_id, member_pwd, months_back=1)
            except GasLoginError as err:
                _LOGGER.warning("가스 로그인 검증 실패: %s", err)
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("가스 로그인 검증 중 알 수 없는 오류")
                errors["base"] = "cannot_connect"
            else:
                self._gas_data = {CONF_GAS_MEMBER_ID: member_id, CONF_GAS_MEMBER_PWD: member_pwd}
                return await self.async_step_kocom_account()

        return self.async_show_form(step_id="user", data_schema=GAS_SCHEMA, errors=errors)

    async def async_step_kocom_account(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """② 코콤 아이디/비밀번호로 서버 주소 자동 조회 + 인증까지 한 화면에서 (옵션의
        재로그인 화면 - async_step_kocom_login과 같은 패턴). 원래는 "아이디만 입력 →
        IP 조회" 화면이 따로 있었는데(kocom_energy 원본 방식), 옵션 쪽은 이미 한
        화면으로 합쳐져 있었던 거라 최초 설정도 똑같이 맞췄다."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._kocom_username = user_input[CONF_KOCOM_USERNAME].strip()
            kocom_password = user_input[CONF_KOCOM_PASSWORD]
            update_interval = user_input[CONF_KOCOM_UPDATE_INTERVAL]

            try:
                ip = await _lookup_kocom_ip(self._kocom_username)
            except aiohttp.ClientError:
                errors["base"] = "cannot_connect"
            except TimeoutError:
                errors["base"] = "timeout"
            except IpAddressNotFoundError:
                errors["base"] = "ip_not_found"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("코콤 서버 IP 조회 중 알 수 없는 오류")
                errors["base"] = "cannot_connect"
            else:
                hashed_username = string_to_padded_hex(md5_hashing(self._kocom_username), 80)
                hashed_password = string_to_padded_hex(md5_hashing(kocom_password), 80)
                fcm = string_to_padded_hex("", 512)
                phone = string_to_padded_hex("", 32)

                api = KocomAPI(ip=ip, username=hashed_username, password=hashed_password, fcm=fcm, phone=phone)
                try:
                    authenticated = await api.authenticate()
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("코콤 인증 중 알 수 없는 오류")
                    authenticated = False

                if authenticated:
                    return self.async_create_entry(
                        title="엘크루 관리비",
                        data={
                            **self._gas_data,
                            CONF_KOCOM_IP: ip,
                            CONF_KOCOM_USERNAME: hashed_username,
                            CONF_KOCOM_PASSWORD: hashed_password,
                            "kocom_fcm": fcm,
                            "kocom_phone": phone,
                            CONF_KOCOM_UPDATE_INTERVAL: update_interval,
                            "kocom_original_username": self._kocom_username,
                        },
                    )
                errors["base"] = "auth_error"

        return self.async_show_form(
            step_id="kocom_account",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_KOCOM_USERNAME, default=self._kocom_username): str,
                    vol.Required(CONF_KOCOM_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                    vol.Required(
                        CONF_KOCOM_UPDATE_INTERVAL, default=DEFAULT_KOCOM_UPDATE_INTERVAL
                    ): vol.All(vol.Coerce(int), vol.Range(min=0)),
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return VnrisaOptionsFlow()


class VnrisaOptionsFlow(OptionsFlow):
    """설치 이후 아이디/비밀번호가 바뀌면(비밀번호 변경 등) 통합을 통째로 삭제하고
    다시 추가할 필요 없이, 옵션에서 바로 재입력/재검증할 수 있게 메뉴로 나눈다."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["bill_info", "gas_login", "citywaste_login", "kocom_login"],
        )

    async def async_step_bill_info(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """관리비(vnrisa)는 로그인도 없고 고정 조회 주기도 없다 - 그 달 고지서
        링크를 "고지서 링크" 텍스트 입력창에 붙여넣을 때만 조회하는 이벤트성 구조라,
        바꿀 수 있는 설정이 없는 안내 전용 화면(제출해도 아무 값도 안 바뀜)."""
        if user_input is not None:
            return self.async_create_entry(data=dict(self.config_entry.options))
        return self.async_show_form(step_id="bill_info", data_schema=vol.Schema({}))

    async def async_step_gas_login(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """도시가스 비밀번호가 바뀌었을 때 등 - 로그인 검증까지 하고 entry.data를
        갈아치운다(옵션이 아니라 로그인 정보라 entry.data 쪽). 확인시각(옵션)도 같은
        화면에서 같이 받는다 - 둘 다 "도시가스" 관련 설정인데 메뉴를 따로 둘 이유가
        없었다."""
        errors: dict[str, str] = {}
        current_check_time = self.config_entry.options.get(CONF_GAS_CHECK_TIME, DEFAULT_GAS_CHECK_TIME)
        if user_input is not None:
            member_id = user_input[CONF_GAS_MEMBER_ID].strip()
            member_pwd = user_input[CONF_GAS_MEMBER_PWD]
            check_time = user_input[CONF_GAS_CHECK_TIME].strip()
            if not _TIME_RE.match(check_time):
                errors[CONF_GAS_CHECK_TIME] = "invalid_time"
            else:
                try:
                    await fetch_gas_history(member_id, member_pwd, months_back=1)
                except GasLoginError as err:
                    _LOGGER.warning("가스 로그인 검증 실패: %s", err)
                    errors["base"] = "invalid_auth"
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("가스 로그인 검증 중 알 수 없는 오류")
                    errors["base"] = "cannot_connect"
                else:
                    self.hass.config_entries.async_update_entry(
                        self.config_entry,
                        data={
                            **self.config_entry.data,
                            CONF_GAS_MEMBER_ID: member_id,
                            CONF_GAS_MEMBER_PWD: member_pwd,
                        },
                    )
                    # entry.data만 바꾸면(옵션이 아니라) update_listener가 안 불려서 다음
                    # 재시작 전까진 새 로그인 정보가 실제로 쓰이지 않는다 - 바로 반영되게
                    # 명시적으로 reload.
                    self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
                    return self.async_create_entry(
                        data={**self.config_entry.options, CONF_GAS_CHECK_TIME: check_time}
                    )

        return self.async_show_form(
            step_id="gas_login",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_GAS_MEMBER_ID, default=self.config_entry.data.get(CONF_GAS_MEMBER_ID, "")
                    ): str,
                    vol.Required(CONF_GAS_MEMBER_PWD): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                    vol.Required(CONF_GAS_CHECK_TIME, default=current_check_time): str,
                }
            ),
            errors=errors,
        )

    async def async_step_kocom_login(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """코콤 비밀번호가 바뀌었을 때 등 - 최초 설정 때처럼 아이디/비밀번호만 받고,
        IP는 입력칸 없이 아이디로 자동 조회해서 인증 검증 후 entry.data를 갈아치운다."""
        errors: dict[str, str] = {}
        if user_input is not None:
            kocom_username = user_input[CONF_KOCOM_USERNAME]
            kocom_password = user_input[CONF_KOCOM_PASSWORD]
            update_interval = user_input[CONF_KOCOM_UPDATE_INTERVAL]

            try:
                ip = await _lookup_kocom_ip(kocom_username)
            except (aiohttp.ClientError, TimeoutError, IpAddressNotFoundError) as err:
                _LOGGER.warning("코콤 서버 IP 조회 실패: %s", err)
                errors["base"] = "ip_not_found"
            else:
                hashed_username = string_to_padded_hex(md5_hashing(kocom_username), 80)
                hashed_password = string_to_padded_hex(md5_hashing(kocom_password), 80)
                fcm = string_to_padded_hex("", 512)
                phone = string_to_padded_hex("", 32)

                api = KocomAPI(ip=ip, username=hashed_username, password=hashed_password, fcm=fcm, phone=phone)
                try:
                    authenticated = await api.authenticate()
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("코콤 인증 중 알 수 없는 오류")
                    authenticated = False

                if authenticated:
                    self.hass.config_entries.async_update_entry(
                        self.config_entry,
                        data={
                            **self.config_entry.data,
                            CONF_KOCOM_IP: ip,
                            CONF_KOCOM_USERNAME: hashed_username,
                            CONF_KOCOM_PASSWORD: hashed_password,
                            "kocom_fcm": fcm,
                            "kocom_phone": phone,
                            CONF_KOCOM_UPDATE_INTERVAL: update_interval,
                            "kocom_original_username": kocom_username,
                        },
                    )
                    # IP/비밀번호는 다음 폴링 때 entry.data를 다시 읽어 바로 반영되지만,
                    # 조회 주기는 코디네이터 생성 시 한 번만 읽혀서 reload 없인 다음
                    # 재시작 전까지 안 바뀐다 - 명시적으로 reload.
                    self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
                    return self.async_create_entry(data=dict(self.config_entry.options))
                errors["base"] = "auth_error"

        data = self.config_entry.data
        return self.async_show_form(
            step_id="kocom_login",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_KOCOM_USERNAME, default=data.get("kocom_original_username", "")
                    ): str,
                    vol.Required(CONF_KOCOM_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                    vol.Required(
                        CONF_KOCOM_UPDATE_INTERVAL,
                        default=data.get(CONF_KOCOM_UPDATE_INTERVAL, DEFAULT_KOCOM_UPDATE_INTERVAL),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0)),
                }
            ),
            errors=errors,
        )

    async def async_step_citywaste_login(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """음쓰(citywaste.or.kr) 로그인 - 기존 설치에 새로 추가하는 용도라
        여기가 최초 입력 지점이다(처음 설치할 때는 이 항목 자체가 없었음)."""
        errors: dict[str, str] = {}
        data = self.config_entry.data
        if user_input is not None:
            member_id = user_input[CONF_CITYWASTE_ID].strip()
            member_pwd = user_input[CONF_CITYWASTE_PWD]
            refresh_interval = user_input[CONF_CITYWASTE_REFRESH_INTERVAL]
            try:
                await fetch_citywaste_history(member_id, member_pwd, months_back=1)
            except CitywasteLoginError as err:
                _LOGGER.warning("음쓰(citywaste) 로그인 검증 실패: %s", err)
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("음쓰(citywaste) 로그인 검증 중 알 수 없는 오류")
                errors["base"] = "cannot_connect"
            else:
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data={
                        **self.config_entry.data,
                        CONF_CITYWASTE_ID: member_id,
                        CONF_CITYWASTE_PWD: member_pwd,
                        CONF_CITYWASTE_REFRESH_INTERVAL: refresh_interval,
                    },
                )
                self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
                return self.async_create_entry(data=dict(self.config_entry.options))

        return self.async_show_form(
            step_id="citywaste_login",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CITYWASTE_ID, default=data.get(CONF_CITYWASTE_ID, "")
                    ): str,
                    vol.Required(CONF_CITYWASTE_PWD): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                    vol.Required(
                        CONF_CITYWASTE_REFRESH_INTERVAL,
                        default=data.get(CONF_CITYWASTE_REFRESH_INTERVAL, DEFAULT_CITYWASTE_REFRESH_INTERVAL),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0)),
                }
            ),
            errors=errors,
        )
