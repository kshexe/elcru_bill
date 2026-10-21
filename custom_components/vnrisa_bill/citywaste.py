"""citywaste.or.kr(RFID 음식물쓰레기 종량기) 조회 - 두 가지 소스를 섞어 쓴다.

1) "나의 수수료 조회"(로그인 필요) - 이미 끝난 달의 확정된 정산금액/배출량/배출건수.
   이번달(진행 중)은 아직 정산 배치가 안 돌아서 전부 0으로 나온다.
2) selectDischargerQuantityQuickMonthNew.do - 로그인조차 필요 없이 RFID 태그ID+동+호수
   만으로 "이번달 1일부터 지금까지"의 배출 건별 내역(건마다 kg)을 실시간으로 준다.
   이미 설치돼 있던 oukene/citywaste HACS 컴포넌트가 바로 이 API만 쓰고 있었다
   (로그인 없이 그 3개 값만 설정에서 입력받는 방식) - 실측 비교로 수치 일치 확인됨.

그래서 이번달은 2)를 써서 실시간 배출량/배출건수를 보여주고, 그 전 달들은 1)의 확정
값을 쓴다. 태그ID는 로그인 응답(membInfo.tagprintcd)에서, 동/호수는 "나의 수수료
조회" 페이지의 "배출원 : ○○아파트 102-1302" 문구에서 그대로 뽑아 쓴다 - 따로 입력받을
필요가 없다.
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
QUICK_MONTH_URL = f"{BASE}/portal/status/selectDischargerQuantityQuickMonthNew.do"
QUICK_MONTH_REFERER = f"{BASE}/portal/status/selectSimpleEmissionQuantity.do"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14; SM-S918N) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36"
    ),
}

_DISCHARGE_ORIGIN_RE = re.compile(r"배출원\s*:\s*.*?(\d+)\s*-\s*(\d+)")

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
    """정산금액/배출량/배출건수(+동/호수)를 뽑는다. 해당 달 데이터가 없으면 None."""
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
    origin_match = _DISCHARGE_ORIGIN_RE.search(soup.get_text())
    if origin_match:
        result["dong"], result["ho"] = origin_match.group(1), origin_match.group(2)
    return result


async def _login(session: aiohttp.ClientSession, member_id: str, member_pwd: str) -> dict:
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
    return result.get("membInfo") or {}


async def _fetch_current_month_live(
    session: aiohttp.ClientSession, tag_id: str, dong: str, ho: str
) -> dict | None:
    """selectDischargerQuantityQuickMonthNew.do - 로그인 없이도 태그ID+동+호수만으로
    이번달 1일부터 지금까지의 배출 건별 내역(건마다 kg)을 바로 준다. 이미 설치돼 있던
    oukene/citywaste 컴포넌트가 쓰던 바로 그 API(실측으로 수치 일치 확인됨) - 정산
    배치가 아직 안 돈 이번달의 실시간 배출량/배출건수를 보여주는 용도.

    한 페이지가 10건이라 배출이 그보다 많은 달엔 여러 페이지로 나뉜다 - staiji/
    citywaste_korea(github) 참고해서 totalPageCount만큼 다 돌며 합산한다."""
    today = date.today()
    params = {
        "tagprintcd": tag_id,
        "aptdong": dong,
        "apthono": ho,
        "startchdate": today.strftime("%Y%m01"),
        "endchdate": today.strftime("%Y%m%d"),
    }
    total_kg = 0.0
    total_count = 0
    page = 1
    total_pages = 1
    try:
        while page <= total_pages:
            async with session.get(
                QUICK_MONTH_URL,
                params={**params, "pageIndex": page},
                headers={"Referer": QUICK_MONTH_REFERER},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
            if page == 1:
                total_count = data.get("totalCnt", 0)
                total_pages = (data.get("paginationInfo") or {}).get("totalPageCount", 1)
            for item in data.get("list") or []:
                total_kg += item.get("qtyvalue", 0)
            page += 1
    except (aiohttp.ClientError, ValueError):
        return None
    return {"discharge_kg": round(total_kg, 2), "discharge_count": total_count}


def _month_back(base: date, months_back: int) -> tuple[int, int]:
    total = base.year * 12 + (base.month - 1) - months_back
    year, month = divmod(total, 12)
    return year, month + 1


async def fetch_citywaste_history(member_id: str, member_pwd: str, months_back: int = 1) -> list[dict]:
    """로그인 후 최근 `months_back`개월치(이번달 포함) 음식물쓰레기 정산내역을 달마다
    따로 조회한다. 그 달 데이터가 아직 없으면(예: 이번달 중간) 조용히 건너뛴다.

    이번달(offset 0)은 정산 배치가 아직 안 돌아서 "나의 수수료 조회"가 0만 주는 경우가
    많다 - 그 경우 로그인 때 받은 태그ID + 이 페이지에서 뽑은 동/호수로 실시간 조회
    API를 한 번 더 불러서 덮어쓴다."""
    rows: list[dict] = []
    today = date.today()
    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(unsafe=True), headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)
    ) as session:
        memb_info = await _login(session, member_id, member_pwd)
        tag_id = memb_info.get("tagprintcd")
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
            if offset == 0 and tag_id and parsed.get("dong") and parsed.get("ho"):
                live = await _fetch_current_month_live(session, tag_id, parsed["dong"], parsed["ho"])
                if live is not None:
                    parsed.update(live)
            rows.append(parsed)
    return rows
