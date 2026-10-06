"""기사 텍스트 → 상장 종목 코드 매핑 (Aho-Corasick 사전 매칭).

네이버 종목뉴스 태그는 종목당 최근 2,000건까지만 조회되므로, 그보다 오래된 기사는
본문에 나온 회사명으로 종목을 연결한다. 정밀도를 위해:
  - 왼쪽 경계: 앞 글자가 한글/영숫자가 아니어야 한다 ("이마트" 안의 "마트" 방지).
    오른쪽은 조사가 붙으므로 검사하지 않는다 ("삼성전자가").
  - 같은 위치에서 겹치면 가장 긴 이름만 ("삼성전자우" > "삼성전자").
  - 2글자 이하 이름(SK, LG, 한화, 대상 …)은 그룹명·일반명사와 겹치므로 제목에 나올 때만 인정.
  - 본문에 6자리 종목코드가 괄호 안에 나오면 그대로 인정.
매핑 방식은 메타데이터(stock_match)로 남겨 검색 시 가중치를 다르게 줄 수 있게 한다.
"""
from __future__ import annotations

import json
import re

import ahocorasick

from .config import SNAPSHOT

_HANGUL_ALNUM = re.compile(r"[가-힣A-Za-z0-9]")
_CODE = re.compile(r"\((\d{6})\)")


class StockMatcher:
    def __init__(self, stocks: list[dict] | None = None):
        if stocks is None:
            stocks = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["stocks"]
        self.codes = {s["code"] for s in stocks}
        self.ac = ahocorasick.Automaton()
        for s in stocks:
            name = s["name"].strip()
            if name:
                self.ac.add_word(name, (name, s["code"]))
        self.ac.make_automaton()

    def _scan(self, text: str):
        hits = []
        for end, (name, code) in self.ac.iter(text):
            start = end - len(name) + 1
            if start > 0 and _HANGUL_ALNUM.match(text[start - 1]):
                continue
            hits.append((start, end, name, code))
        # 겹치는 매치 중 가장 긴 것만
        hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
        out, last_end = [], -1
        for h in hits:
            if h[0] > last_end:
                out.append(h)
                last_end = h[1]
        return out

    def match(self, title: str, body: str = "") -> dict[str, str]:
        """{code: 방식} - 방식은 code | title | body"""
        found: dict[str, str] = {}
        for c in _CODE.findall(title + "\n" + body):
            if c in self.codes:
                found[c] = "code"
        for _, _, name, code in self._scan(title):
            found.setdefault(code, "title")
        for _, _, name, code in self._scan(body):
            if len(name) <= 2:
                continue
            found.setdefault(code, "body")
        return found
