"""붙여넣은 고지서 URL을 그대로 조회한다. 예약 폴링이 없다 - 같은 URL을 다시 조회해봐야
같은 달 고지서가 그대로 나올 뿐이라(새 Token 없이는 다음 달로 넘어갈 수 없음), 주기적으로
재조회할 이유 자체가 없다."""
from __future__ import annotations

from typing import Any

import aiohttp

from .parser import parse_bill

# 고지서는 모바일 웹페이지. HA 기본 User-Agent로는 다른 페이지가 올 수 있어 모바일 브라우저로 위장
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14; SM-S918N) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9",
}


def _decode(raw: bytes, charset: str | None) -> str:
    for enc in filter(None, (charset, "utf-8", "cp949")):
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


async def fetch_bill(url: str) -> dict[str, Any]:
    """카카오 알림톡으로 받은 고지서 링크(Token 포함)를 그대로 조회해서 파싱한다."""
    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(unsafe=True),
        headers=HEADERS,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as session:
        async with session.get(url, allow_redirects=True, max_redirects=10) as resp:
            resp.raise_for_status()
            raw = await resp.read()
            html = _decode(raw, resp.charset)
            final_url = str(resp.url)

    data = parse_bill(html, final_url)
    data["url"] = final_url
    return data
