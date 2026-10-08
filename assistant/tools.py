"""AI 비서가 고를 수 있는 도구. 각 도구는 결과를 '근거(Evidence)' 하나로 돌려주고, 답변은 다른 근거처럼 [번호]로 인용한다.

  quote          지금 시세 (사이트 /api/quote, 야후 파이낸스 지연 시세)
  fundamentals   투자 지표 (사이트 일일 스냅샷: 시가총액·PER·PBR·EPS·BPS·배당수익률·외국인 보유율·52주 범위·업종)
  price_history  기간 주가 흐름 (사이트 /api/chart 일봉: 기간 수익률·최고·최저)
  screen         조건 검색 (스냅샷 전 종목: 업종·지표 조건·정렬)
  relations      회사 관계 (지식 그래프: 최대주주·종속회사·계열회사·임원) - kg.py

도구 결과는 모두 사실 데이터다. 해석·매매 판단은 하지 않는다(답변 규칙).
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

import requests

from retrieval import KST, Evidence

SITE = "https://moastock.co.kr"
_http = requests.Session()
_http.headers["User-Agent"] = "moastock-assistant"
_snap = {"at": 0.0, "data": None}
_lock = threading.Lock()

FIELDS = {   # 스냅샷 필드 → (이름, 단위)
    "price": ("종가", "원"), "change": ("등락률", "%"), "marketCap": ("시가총액", "원"), "per": ("PER", "배"),
    "pbr": ("PBR", "배"), "eps": ("EPS", "원"), "bps": ("BPS", "원"), "dividendYield": ("배당수익률", "%"),
    "foreignRate": ("외국인 보유율", "%"), "high52w": ("52주 최고가", "원"), "low52w": ("52주 최저가", "원"),
    "volume": ("거래량", "주"), "value": ("거래대금", "원"),
}


def snapshot() -> dict:
    """사이트 일일 스냅샷 (10분 캐시)"""
    with _lock:
        if _snap["data"] is None or time.time() - _snap["at"] > 600:
            r = _http.get(f"{SITE}/data/snapshot.json", timeout=20)
            r.raise_for_status()
            d = r.json()
            d["by_code"] = {s["code"]: s for s in d["stocks"]}
            _snap.update(at=time.time(), data=d)
        return _snap["data"]


def won(v: float) -> str:
    """큰 원화 금액을 '1,596조 341억원'처럼 (정확한 값, 억 단위 아래는 버림)"""
    v = int(v)
    jo, eok = v // 10**12, (v % 10**12) // 10**8
    if jo:
        return f"{jo:,}조 {eok:,}억원" if eok else f"{jo:,}조원"
    if eok:
        return f"{eok:,}억원"
    return f"{v:,}원"


def fmt(field: str, v) -> str:
    if v is None:
        return "-"
    name, unit = FIELDS[field]
    if field in ("marketCap", "value"):
        return won(v)
    if unit == "%":
        return f"{v:+.2f}%" if field == "change" else f"{v:.2f}%"
    if unit == "배":
        return f"{v:.2f}배"
    return f"{v:,.0f}{unit}"


def _ev(name: str, title: str, text: str, url: str, at: datetime | None) -> Evidence:
    return Evidence(n=0, chunk_id="", doc_id=f"tool:{name}", text=text, score=1.0, title=title, publisher="모아스톡",
                    published_at=at, url=url, original_url=None, source_type="data", report_nm=None, kind="data")


def _stock(code: str) -> dict | None:
    return snapshot()["by_code"].get(code)


def _symbol(s: dict) -> str:
    return f"{s['code']}.{'KQ' if s.get('market') == 'KOSDAQ' else 'KS'}"


def _snap_time() -> datetime | None:
    g = snapshot().get("generatedAt")
    try:
        return datetime.fromisoformat(g.replace("Z", "+00:00")).astimezone(KST) if g else None
    except ValueError:
        return None


# ---- 도구 ---------------------------------------------------------------------------

def quote(codes: list[str]) -> list[Evidence]:
    stocks = [s for c in codes if (s := _stock(c))]
    if not stocks:
        return []
    r = _http.get(f"{SITE}/api/quote", params={"symbols": ",".join(_symbol(s) for s in stocks)}, timeout=20)
    r.raise_for_status()
    by_sym = {q["symbol"]: q for q in r.json().get("quotes", [])}
    out = []
    for s in stocks:
        q = by_sym.get(_symbol(s))
        if not q or q.get("price") is None:
            continue
        at = datetime.fromtimestamp(q["time"], KST) if q.get("time") else datetime.now(KST)
        text = (f"{s['name']}({s['code']}) 시세 - 조회 시각 {at:%Y-%m-%d %H:%M} (지연 시세일 수 있음)\n"
                f"현재가 {q['price']:,.0f}원, 전일 종가 {q['prevClose']:,.0f}원, 전일 대비 {q['changePct']:+.2f}%")
        out.append(_ev("quote", f"{s['name']} 시세", text, f"{SITE}/stock.html?code={s['code']}", at))
    return out


def fundamentals(codes: list[str]) -> list[Evidence]:
    at = _snap_time()
    out = []
    for c in codes:
        s = _stock(c)
        if not s:
            continue
        lines = [f"{s['name']}({s['code']}, {s.get('market')}, 업종 {s.get('industry') or '-'}) 투자 지표 - "
                 f"기준 {at:%Y-%m-%d} 장 마감 스냅샷" if at else f"{s['name']}({s['code']}) 투자 지표"]
        lines.append(" · ".join(f"{FIELDS[f][0]} {fmt(f, s.get(f))}" for f in
                                ("price", "change", "marketCap", "per", "pbr", "eps", "bps", "dividendYield",
                                 "foreignRate", "high52w", "low52w")))
        out.append(_ev("fundamentals", f"{s['name']} 투자 지표", "\n".join(lines),
                       f"{SITE}/stock.html?code={s['code']}", at))
    return out


def price_history(codes: list[str], days: int = 30) -> list[Evidence]:
    days = max(5, min(int(days or 30), 365))
    out = []
    for c in codes:
        s = _stock(c)
        if not s:
            continue
        r = _http.get(f"{SITE}/api/chart", params={"symbol": _symbol(s), "tf": "day"}, timeout=20)
        r.raise_for_status()
        candles = [k for k in r.json().get("candles", []) if k.get("close")]
        if len(candles) < 2:
            continue
        end = datetime.fromtimestamp(candles[-1]["time"], KST)
        since = end - timedelta(days=days)
        win = [k for k in candles if datetime.fromtimestamp(k["time"], KST) >= since] or candles[-2:]
        first, last = win[0], win[-1]
        hi = max(win, key=lambda k: k["high"])
        lo = min(win, key=lambda k: k["low"])
        d = lambda k: datetime.fromtimestamp(k["time"], KST).strftime("%Y-%m-%d")  # noqa: E731
        ret = (last["close"] / first["close"] - 1) * 100
        text = (f"{s['name']}({s['code']}) 최근 {days}일 일봉 - {d(first)} ~ {d(last)} ({len(win)}거래일)\n"
                f"시작 종가 {first['close']:,.0f}원 → 마지막 종가 {last['close']:,.0f}원, 기간 수익률 {ret:+.2f}%\n"
                f"기간 최고가 {hi['high']:,.0f}원({d(hi)}), 기간 최저가 {lo['low']:,.0f}원({d(lo)})")
        out.append(_ev("price_history", f"{s['name']} 최근 {days}일 주가", text,
                       f"{SITE}/stock.html?code={s['code']}", end))
    return out


SCREEN_FIELDS = {"per", "pbr", "dividendYield", "marketCap", "foreignRate", "change", "eps", "price", "value"}


def screen(industry: str | None = None, filters: list[dict] | None = None, sort: str | None = None,
           order: str = "desc", market: str | None = None, limit: int = 10) -> list[Evidence]:
    """조건에 맞는 종목 목록. filters: [{"field": "per", "op": "<=", "value": 10}, ...]"""
    stocks = snapshot()["stocks"]
    conds = []
    for f in filters or []:
        if f.get("field") in SCREEN_FIELDS and f.get("op") in ("<=", ">=", "<", ">") and f.get("value") is not None:
            conds.append((f["field"], f["op"], float(f["value"])))
    ops = {"<=": float.__le__, ">=": float.__ge__, "<": float.__lt__, ">": float.__gt__}
    rows = []
    for s in stocks:
        if market and s.get("market") != market:
            continue
        if industry and industry not in (s.get("industry") or ""):
            continue
        if any(s.get(f) is None or not ops[op](float(s[f]), v) for f, op, v in conds):
            continue
        if any(s.get(f) is not None and f in ("per", "pbr") and s[f] <= 0 for f, _, _ in conds):
            continue                                    # 적자(음수 PER) 종목은 'PER 낮은 순'에서 뺀다
        rows.append(s)
    sort = sort if sort in SCREEN_FIELDS else ("marketCap" if not conds else conds[0][0])
    rows = [s for s in rows if s.get(sort) is not None]
    rows.sort(key=lambda s: s[sort], reverse=(order != "asc"))
    total = len(rows)
    rows = rows[:max(1, min(int(limit or 10), 15))]
    if not rows:
        return []
    at = _snap_time()
    desc = []
    if industry:
        desc.append(f"업종에 '{industry}' 포함")
    if market:
        desc.append(market)
    desc += [f"{FIELDS[f][0]} {op} {v:g}{'%' if FIELDS[f][1] == '%' else ''}" for f, op, v in conds]
    head = (f"조건 검색 ({', '.join(desc) or '조건 없음'}) - {FIELDS[sort][0]} {'낮은' if order == 'asc' else '높은'} 순, "
            f"해당 {total}종목 중 {len(rows)}개" + (f", 기준 {at:%Y-%m-%d} 스냅샷" if at else ""))
    lines = [head] + [f"{i}. {s['name']}({s['code']}, {s.get('industry') or '-'}) — " +
                      " · ".join(f"{FIELDS[f][0]} {fmt(f, s.get(f))}" for f in
                                 dict.fromkeys([sort, "per", "pbr", "dividendYield", "marketCap"]))
                      for i, s in enumerate(rows, 1)]
    return [_ev("screen", "모아스톡 스크리너 조건 검색", "\n".join(lines), f"{SITE}/screener.html", at)]
