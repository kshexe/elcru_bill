"""citywaste.or.kr(RFID 음식물쓰레기 종량기) 로그인 조회 - "나의 수수료 조회" 페이지에서
달별 정산금액/배출량/배출건수를 가져온다.

GSE와 달리 "한 번에 전체 기간"을 주는 API가 없고, 달마다 조회 페이지를 다시 요청해야
한다(폼의 searchYYYY/searchMM을 바꿔서 같은 URL에 POST). 로그인하면 "비밀번호 변경"
페이지로 유도하는 메시지(msgCode=="PW")가 오지만, 그건 로그인 페이지의 안내일 뿐 세션
자체는 이미 유효해서 다른 페이지 접근에는 문제가 없다 - 실측으로 확인됨.
"""
from __future__ import annotations

import re
from datetime import date

import aiohttp
from bs4 import BeautifulSoup

BASE = "https://www.citywaste.or.kr"
LOGIN_URL = f"{BASE}/actionLoginNew.do"
LOGIN_PAGE_URL = f"{BASE}/ucwmsNew/member/memberLogin.do"
CHARGE_URL = f"{BASE}/portal/status/selectMyEmissionCharge.do"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14; SM-S918N) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36"
    ),
}

# 로그인 실패를 뜻하는 코드만 에러로 본다 - "PW"(비밀번호 변경 권고)는 로그인 자체는
# 성공이라 그대로 진행한다.
_LOGIN_ERROR_CODES = {"E", "MEMUSEYN"}


class CitywasteLoginError(Exception):
    """아이디/비밀번호가 틀렸거나 로그인에 실패했을 때."""


def _num(text: str) -> float | int | None:
    text = text.strip().replace(",", "")
    m = re.match(r"^(-?\d+(?:\.\d+)?)", text)
    if not m:
        return None
    raw = m.group(1)
    return float(raw) if "." in raw else int(raw)


def _parse_charge_page(html: str) -> dict | None:
    """정산금액/배출량/배출건수를 뽑는다. 해당 달 데이터가 없으면 None."""
    soup = BeautifulSoup(html, "html.parser")
    amount_el = soup.select_one("div.total_amount span.num")
    if amount_el is None:
        return None
    result: dict = {"final_amount": _num(amount_el.get_text())}
    for th in soup.find_all("th"):
        label = th.get_text(strip=True)
        td = th.find_next_sibling("td")
        if td is None:
            continue
        value = td.get_text(strip=True)
        if label == "배출량":
            result["discharge_kg"] = _num(value)
        elif label == "배출건수":
            result["discharge_count"] = _num(value)
    return result


async def _login(session: aiohttp.ClientSession, member_id: str, member_pwd: str) -> None:
    async with session.get(LOGIN_PAGE_URL) as resp:
        resp.raise_for_status()
        await resp.text()
    async with session.post(
        LOGIN_URL,
        data={"userid": member_id, "userpwd": member_pwd},
        headers={"Referer": LOGIN_PAGE_URL, "X-Requested-With": "XMLHttpRequest"},
    ) as resp:
        resp.raise_for_status()
        result = await resp.json(content_type=None)
    if result.get("msgCode") in _LOGIN_ERROR_CODES:
        raise CitywasteLoginError(result.get("msg") or "로그인 실패 - 아이디/비밀번호를 확인하세요")


def _month_back(base: date, months_back: int) -> tuple[int, int]:
    total = base.year * 12 + (base.month - 1) - months_back
    year, month = divmod(total, 12)
    return year, month + 1


async def fetch_citywaste_history(member_id: str, member_pwd: str, months_back: int = 1) -> list[dict]:
    """로그인 후 최근 `months_back`개월치(이번달 포함) 음식물쓰레기 정산내역을 달마다
    따로 조회한다. 그 달 데이터가 아직 없으면(예: 이번달 중간) 조용히 건너뛴다."""
    rows: list[dict] = []
    today = date.today()
    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(unsafe=True), headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)
    ) as session:
        await _login(session, member_id, member_pwd)
        for offset in range(months_back):
            year, month = _month_back(today, offset)
            async with session.post(
                CHARGE_URL,
                data={"searchYYYY": str(year), "searchMM": f"{month:02d}"},
                headers={"Referer": CHARGE_URL},
            ) as resp:
                resp.raise_for_status()
                html = await resp.text()
            parsed = _parse_charge_page(html)
            if parsed is None:
                continue
            parsed["month"] = f"{year:04d}-{month:02d}"
            rows.append(parsed)
    return rows
