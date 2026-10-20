"""vnrisa(엘크루 관리비) HTML 파서.

테이블 구조를 위치(열 순서) 기준으로 읽어서 항목명을 하드코딩하지 않는다.
- 부과 내역: [그룹] 항목 | 당월 | 전월 | (전월차액) | 전년동월 | (전년차액)
- 사용량: 항목명이 "전기 924 k", "수도 15 t" 형태면 사용량으로 분리
- 계량기 지침: ⊙전기/⊙냉수... 헤더 + 당월/전월 행
"""
from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

_NUM_RE = re.compile(r"^\(?([+-]?\d[\d,]*(?:\.\d+)?)\)?$")
_USAGE_RE = re.compile(r"^(\D+?)\s*([\d,]+(?:\.\d+)?)\s*(kwh|k|t|톤|㎥|m3)?$", re.I)
_UNIT_MAP = {"k": "kWh", "kwh": "kWh", "t": "m³", "톤": "m³", "㎥": "m³", "m3": "m³"}
_MARKER_RE = re.compile(r"^[▶⊙①②③④⑤⑥⑦⑧⑨\s]+")

# 표 밖(본문 텍스트)에 있는 금액 항목
SUMMARY_LABELS = [
    "납부하실금액", "당월부과금액", "실납부금액", "미납관리비", "미납연체료",
    "납기후연체료", "납기후금액", "미납총액",
]
DETAIL_LABELS = [
    "기후환경요금", "연료비조정액", "전기복지할인", "전기대가족할인", "수도감면",
    "필수보장공제", "에너지캐쉬백", "에너지수비대",
]


def _num(text: str) -> int | float | None:
    m = _NUM_RE.match(text.replace(" ", ""))
    if not m:
        return None
    raw = m.group(1).replace(",", "")
    return float(raw) if "." in raw else int(raw)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ").replace("　", " ")).strip()


def _key(text: str) -> str:
    return re.sub(r"\s+", "", _MARKER_RE.sub("", text))


def parse_bill(html: str, url: str = "") -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    lines = [_clean(x) for x in soup.get_text("\n").splitlines()]
    text = "\n".join(x for x in lines if x)

    data: dict[str, Any] = {
        "month": None, "apartment": None, "unit": None, "due_date": None,
        "summary": {}, "details": {}, "charges": {}, "usage": {}, "meters": {},
    }

    if m := re.search(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*분", text):
        data["month"] = f"{m.group(1)}-{int(m.group(2)):02d}"
    elif m := re.search(r"YYYYMM=(\d{4})(\d{2})", url, re.I):
        data["month"] = f"{m.group(1)}-{m.group(2)}"
    if m := re.search(r"월분\s*\n([^\n]+)", text):
        data["apartment"] = m.group(1).strip()
    if m := re.search(r"(\d+\s*동\s*\d+\s*호)", text):
        data["unit"] = re.sub(r"\s+", "", m.group(1)).replace("동", "동 ")
    if m := re.search(r"마감일\D{0,6}(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", text):
        data["due_date"] = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    for target, labels in (("summary", SUMMARY_LABELS), ("details", DETAIL_LABELS)):
        for label in labels:
            if m := re.search(rf"{label}[ \t]*\n?[ \t]*(-?\d[\d,]*)", text):
                data[target][label] = int(m.group(1).replace(",", ""))

    _parse_tables(soup, data)
    return data


def _parse_tables(soup: BeautifulSoup, data: dict[str, Any]) -> None:
    charges = data["charges"]
    meter_names: list[str] = []
    group: str | None = None
    last_table = None

    for tr in soup.find_all("tr"):
        if tr.find("table"):
            continue  # 레이아웃용 바깥 행
        table = tr.find_parent("table")
        if table is not last_table:
            last_table, group = table, None

        cells = [_clean(c.get_text(" ")) for c in tr.find_all(["td", "th"], recursive=False)]
        if not any(cells):
            continue

        # 계량기 지침 헤더 (⊙전기 ⊙냉수 ⊙온수)
        heads = [c.lstrip("⊙").strip() for c in cells if c.startswith("⊙")]
        if heads:
            meter_names = heads
            continue
        if cells[0] in ("당월", "전월") and meter_names:
            idx = 0
            for j, c in enumerate(cells):
                if c in ("당월", "전월"):
                    val = _num(cells[j + 1]) if j + 1 < len(cells) else None
                    if idx < len(meter_names) and val is not None:
                        data["meters"].setdefault(meter_names[idx], {})[c] = val
                    idx += 1
            continue

        # 부과 내역 행: 첫 숫자 칸 앞이 라벨
        k = next((i for i, c in enumerate(cells) if _num(c) is not None), None)
        if not k:
            continue
        labels = [c for c in cells[:k] if c]
        if not labels:
            continue
        if len(labels) > 1:
            group = _key(labels[0])
        raw = labels[-1]

        def col(off: int) -> int | float | None:
            return _num(cells[k + off]) if k + off < len(cells) else None

        if _key(raw) == "소계":
            name = f"{group or '관리비'} 소계"
        elif (u := _USAGE_RE.match(raw)) and u.group(3):
            base = _key(u.group(1))
            data["usage"][base] = {
                "value": _num(u.group(2)),
                "unit": _UNIT_MAP[u.group(3).lower()],
            }
            name = f"세대{base}료"
        else:
            name = _key(raw)

        charges[name] = {
            "value": col(0), "prev": col(1), "prev_diff": col(2),
            "yoy": col(3), "yoy_diff": col(4), "group": group,
        }
