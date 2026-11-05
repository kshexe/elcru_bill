"""GSE(지에스이) 도시가스 조회 - 아이디/비밀번호로 로그인하면 고지/수납내역을 월별로
한 번에 전부 받아올 수 있다(간편조회처럼 고객번호+생년월일로 이번 달 사용량/미납액
하나만 보는 것보다 훨씬 자세함 - 검침코드, 지침, 청구금액, 납부금액, 수납일까지).

v.vnrisa.com 관리비 고지서와 달리 여기도 매달 바뀌는 Token이 없다 - 로그인 세션이
유효한 동안은 계속 자동으로 재조회할 수 있고, 로그인 자체도 매번 새로 하면 되므로
세션 만료를 신경 쓸 필요가 없다(매 조회마다 새로 로그인).
"""
from __future__ import annotations

import re
from datetime import date

import aiohttp
from bs4 import BeautifulSoup

BASE = "https://www.yesgse.com"
LOGIN_URL = f"{BASE}/proc/member.login.proc.asp"
LOGIN_REFERER = f"{BASE}/user/member.login.asp"
HISTORY_URL = f"{BASE}/sub/pay.info.payView.list.asp"
HISTORY_REFERER = f"{BASE}/user/pay.info.payview.asp"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14; SM-S918N) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36"
    ),
}


class GasLoginError(Exception):
    """아이디/비밀번호가 틀렸거나 로그인에 실패했을 때."""


def _ym_months_ago(months: int) -> str:
    today = date.today()
    idx = today.year * 12 + (today.month - 1) - months
    return f"{idx // 12}{idx % 12 + 1:02d}"


def _num(text: str) -> int | None:
    text = text.strip().replace(",", "")
    if not text or text in ("미납", "-"):
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _parse_history(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.select("table.list tbody tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) != 7:
            continue
        month, meter_status, meter_reading, usage, billed, paid, pay_date = cells
        if not re.match(r"^\d{4}-\d{2}$", month):
            continue
        rows.append(
            {
                "month": month,
                "meter_status": meter_status,
                "meter_reading": _num(meter_reading),
                "usage_m3": _num(usage),
                "billed_amount": _num(billed),
                "paid_amount": _num(paid) or 0,
                "payment_date": pay_date if pay_date != "미납" else None,
                "unpaid": pay_date == "미납",
            }
        )
    return rows


async def fetch_gas_history(member_id: str, member_pwd: str, months_back: int = 24) -> list[dict]:
    """로그인 후 최근 `months_back`개월치 고지/수납내역을 전부 가져온다."""
    start_ym = _ym_months_ago(months_back)
    end_ym = date.today().strftime("%Y%m")

    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(unsafe=True), headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)
    ) as session:
        async with session.post(
            LOGIN_URL,
            data={"memberId": member_id, "memberPwd": member_pwd, "rtn": "", "tabN": ""},
            headers={"Referer": LOGIN_REFERER},
        ) as resp:
            resp.raise_for_status()
            login_text = await resp.text()
            final_url = str(resp.url)

        if "alert(" in login_text or "main.asp" not in final_url:
            if m := re.search(r"alert\('([^']+)'\)", login_text):
                raise GasLoginError(m.group(1))
            raise GasLoginError("로그인 실패 - 아이디/비밀번호를 확인하세요")

        async with session.get(
            HISTORY_URL,
            params={"startYM": start_ym, "endYM": end_ym, "company_license": ""},
            headers={"Referer": HISTORY_REFERER},
        ) as resp2:
            resp2.raise_for_status()
            history_html = await resp2.text()

    rows = _parse_history(history_html)
    if not rows:
        raise GasLoginError(f"고지/수납내역을 찾지 못함: {history_html[:200]}")
    return rows
