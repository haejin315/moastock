"""해외·코인 일일 스냅샷 생성기 (국내는 build_snapshot.py).

  python scripts/build_global.py [--only us,jp,coin]

출력 (public/data/):
  us.json    미국: 나스닥·뉴욕증권거래소·아멕스 상장 종목 (시가총액 1억 달러 이상)
             + S&P 500·나스닥 100 편입 여부(tags). 시세·지표는 야후 파이낸스.
  jp.json    일본: 니케이 225 구성 종목. 시세·지표는 야후 파이낸스.
  coins.json 코인: 업비트·빗썸 원화마켓 + 바이낸스 USDT 마켓 (가격은 모두 원화, 바이낸스는 원/달러 환산)
             + 코인게코 시가총액, 국내 거래소의 김치 프리미엄(바이낸스 대비 %).

데이터 출처
  - 종목 목록: 나스닥 스크리너 API(미국 상장 전 종목·업종), 나스닥 100 구성 API, 위키백과(S&P 500·니케이 225 구성)
  - 시세·지표: 야후 파이낸스 v7 quote (PER·PBR·EPS·BPS·배당수익률·52주 범위·시가총액)
  - 코인: 업비트·빗썸 공개 API(원화 시세·52주 범위), 바이낸스 공개 시세 미러(data-api.binance.vision),
          코인게코 공개 API(시가총액), 야후 파이낸스(원/달러 환율)
행 필드는 국내 스냅샷과 같은 이름을 쓴다 (스크리너·수식이 그대로 동작하게).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from selectolax.parser import HTMLParser

OUT = Path(__file__).resolve().parents[1] / "public" / "data"
KST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
US_MIN_CAP = 1e8

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

http = requests.Session()
http.headers.update({"User-Agent": UA})

# 자주 찾는 해외 종목·코인의 한글 이름 (검색용)
KO_NAMES = {
    "AAPL": "애플", "MSFT": "마이크로소프트", "NVDA": "엔비디아", "AMZN": "아마존", "GOOGL": "알파벳(구글) A",
    "GOOG": "알파벳(구글) C", "META": "메타", "TSLA": "테슬라", "BRK-B": "버크셔 해서웨이 B", "AVGO": "브로드컴",
    "TSM": "TSMC", "LLY": "일라이 릴리", "JPM": "JP모건", "V": "비자", "MA": "마스터카드", "NFLX": "넷플릭스",
    "AMD": "AMD", "INTC": "인텔", "QCOM": "퀄컴", "MU": "마이크론", "COST": "코스트코", "WMT": "월마트",
    "KO": "코카콜라", "PEP": "펩시코", "DIS": "디즈니", "NKE": "나이키", "ORCL": "오라클", "CRM": "세일즈포스",
    "ADBE": "어도비", "PLTR": "팔란티어", "IBM": "IBM", "BA": "보잉", "XOM": "엑슨모빌", "UNH": "유나이티드헬스",
    "JNJ": "존슨앤드존슨", "PFE": "화이자", "SBUX": "스타벅스", "MCD": "맥도날드", "ASML": "ASML", "ARM": "ARM",
    "7203.T": "도요타", "6758.T": "소니", "9984.T": "소프트뱅크그룹", "6861.T": "키엔스", "8035.T": "도쿄일렉트론",
    "9983.T": "패스트리테일링(유니클로)", "7974.T": "닌텐도", "6501.T": "히타치", "8306.T": "미쓰비시UFJ",
    "7267.T": "혼다", "6098.T": "리크루트", "4063.T": "신에쓰화학", "6902.T": "덴소", "9432.T": "NTT",
    "7741.T": "호야", "6954.T": "화낙", "8058.T": "미쓰비시상사", "8001.T": "이토추상사", "4502.T": "다케다약품",
}


def get(url, *, params=None, json_=True, retries=4, **kw):
    last = None
    for i in range(retries):
        try:
            r = http.get(url, params=params, timeout=30, **kw)
            if r.status_code == 429:
                raise RuntimeError("429")
            r.raise_for_status()
            return r.json() if json_ else r.text
        except Exception as e:  # noqa: BLE001 - 재시도
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[$,%\s]", "", str(v))
    try:
        return float(s)
    except ValueError:
        return None


# ---- 야후 v7 quote (쿠키·crumb 필요) -----------------------------------------------

_crumb = None


def yahoo_quotes(symbols: list[str], batch: int = 150) -> dict:
    global _crumb
    if _crumb is None:
        http.get("https://fc.yahoo.com", timeout=20)                      # 쿠키 받기 (응답 코드는 무관)
        _crumb = http.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=20).text.strip()
    out = {}
    for i in range(0, len(symbols), batch):
        part = symbols[i:i + batch]
        body = get("https://query1.finance.yahoo.com/v7/finance/quote",
                   params={"symbols": ",".join(part), "crumb": _crumb})
        for q in body.get("quoteResponse", {}).get("result", []):
            out[q["symbol"]] = q
        time.sleep(0.5)
    return out


def from_yahoo(q: dict) -> dict:
    price = q.get("regularMarketPrice")
    vol = q.get("regularMarketVolume")
    dy = q.get("dividendYield")
    if dy is None and q.get("trailingAnnualDividendYield") is not None:
        dy = q["trailingAnnualDividendYield"] * 100
    r = lambda v, d=2: round(v, d) if isinstance(v, (int, float)) else None  # noqa: E731
    return {
        "price": r(price, 4), "change": r(q.get("regularMarketChangePercent")), "volume": vol,
        "value": round(price * vol) if price and vol else None, "marketCap": q.get("marketCap"),
        "per": r(q.get("trailingPE")), "pbr": r(q.get("priceToBook")), "eps": r(q.get("epsTrailingTwelveMonths")),
        "bps": r(q.get("bookValue")), "dividendYield": r(dy), "high52w": q.get("fiftyTwoWeekHigh"),
        "low52w": q.get("fiftyTwoWeekLow"), "currency": q.get("currency"),
    }


# ---- 미국 ----------------------------------------------------------------------------

def yahoo_symbol(s: str) -> str:
    return s.strip().replace("/", "-").replace(".", "-").upper()


def build_us() -> dict:
    listed = {}
    for ex, label in (("nasdaq", "NASDAQ"), ("nyse", "NYSE"), ("amex", "AMEX")):
        rows = get("https://api.nasdaq.com/api/screener/stocks",
                   params={"tableonly": "true", "download": "true", "exchange": ex})["data"]["rows"]
        for r in rows:
            cap = num(r.get("marketCap"))
            name = r.get("name") or ""
            if not cap or cap < US_MIN_CAP or re.search(r"\b(Warrant|Rights?|Units?)\b", name):
                continue
            sym = yahoo_symbol(r["symbol"])
            listed[sym] = {"code": sym, "symbol": sym, "name": re.sub(r"\s+(Common Stock|Class [A-C] Common Stock|"
                                                                        r"Ordinary Shares|American Depositary Shares)\b.*$", "", name).strip(),
                           "market": label, "industry": r.get("industry") or r.get("sector") or None,
                           "sector": r.get("sector") or None, "tags": []}
        print(f"  {label}: 누적 {len(listed)}", flush=True)
    page = HTMLParser(get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", json_=False))
    table = page.css_first("table#constituents")
    sp = {yahoo_symbol(td.text(strip=True)) for tr in (table.css("tr") if table else [])
          if (td := tr.css_first("td"))}
    ndx = {yahoo_symbol(r["symbol"]) for r in
           get("https://api.nasdaq.com/api/quote/list-type/nasdaq100")["data"]["data"]["rows"]}
    print(f"  S&P 500 {len(sp)}종목, 나스닥 100 {len(ndx)}종목", flush=True)
    for sym in sp | ndx:                              # 목록에서 빠진 지수 편입 종목도 넣는다
        listed.setdefault(sym, {"code": sym, "symbol": sym, "name": sym, "market": "NYSE", "industry": None,
                                "sector": None, "tags": []})
    for sym, row in listed.items():
        row["tags"] = [t for t, s in (("sp500", sp), ("ndx", ndx)) if sym in s]
    quotes = yahoo_quotes(list(listed))
    stocks = []
    for sym, row in listed.items():
        q = quotes.get(sym)
        if not q or q.get("regularMarketPrice") is None:
            continue
        if row["name"] == sym:
            row["name"] = q.get("longName") or q.get("shortName") or sym
        row.update(from_yahoo(q))
        if sym in KO_NAMES:
            row["nameKo"] = KO_NAMES[sym]
        stocks.append(row)
    stocks.sort(key=lambda s: -(s.get("marketCap") or 0))
    return {"region": "US", "currency": "USD", "stocks": stocks}


# ---- 일본 ----------------------------------------------------------------------------

def build_jp() -> dict:
    page = HTMLParser(get("https://en.wikipedia.org/wiki/Nikkei_225", json_=False))
    sectors = {}
    for sec in page.css("section"):                   # 업종(h3) 구역마다 구성 종목 목록
        head = next((c for c in sec.iter() if c.tag == "div" and "mw-heading3" in (c.attributes.get("class") or "")),
                    None)
        if head is None:
            continue
        name = (head.css_first("h3").text(strip=True) if head.css_first("h3") else "").strip()
        for li in sec.css("li"):
            for c in re.findall(r"TYO:\s*(\d{4})", li.text()):
                sectors.setdefault(c, name or None)
    for c in re.findall(r"TYO:\s*(\d{4})", page.body.text()):   # 구역 밖에 적힌 종목도 빠뜨리지 않게
        sectors.setdefault(c, None)
    codes = sorted(sectors)
    print(f"  니케이 225: {len(codes)}종목", flush=True)
    quotes = yahoo_quotes([f"{c}.T" for c in codes])
    stocks = []
    for c in codes:
        q = quotes.get(f"{c}.T")
        if not q or q.get("regularMarketPrice") is None:
            continue
        row = {"code": c, "symbol": f"{c}.T", "name": q.get("longName") or q.get("shortName") or c,
               "market": "TSE", "industry": sectors.get(c), "tags": ["n225"]}
        row.update(from_yahoo(q))
        if f"{c}.T" in KO_NAMES:
            row["nameKo"] = KO_NAMES[f"{c}.T"]
        stocks.append(row)
    stocks.sort(key=lambda s: -(s.get("marketCap") or 0))
    return {"region": "JP", "currency": "JPY", "stocks": stocks}


# ---- 코인 ----------------------------------------------------------------------------

STABLE = {"USDT", "USDC", "FDUSD", "TUSD", "DAI", "BUSD", "USDP", "USDE", "PYUSD", "USD1", "EUR", "TRY", "BRL"}


def korean_exchange(base: str, label: str) -> dict:
    """업비트·빗썸 원화마켓 (두 거래소 공개 API 형식이 같다) → {기호: 행}"""
    markets = [m for m in get(f"{base}/v1/market/all", params={"isDetails": "false"}) if m["market"].startswith("KRW-")]
    names = {m["market"][4:]: (m.get("korean_name"), m.get("english_name")) for m in markets}
    syms = [m["market"] for m in markets]
    rows = {}
    for i in range(0, len(syms), 100):
        for t in get(f"{base}/v1/ticker", params={"markets": ",".join(syms[i:i + 100])}):
            sym = t["market"][4:]
            ko, en = names.get(sym, (None, None))
            rows[sym] = {"code": sym, "name": ko or en or sym, "nameEn": en, "market": label,
                         "price": t["trade_price"], "change": round(t["signed_change_rate"] * 100, 2),
                         "volume": t.get("acc_trade_volume_24h"), "value": round(t["acc_trade_price_24h"]),
                         "high52w": t.get("highest_52_week_price"), "low52w": t.get("lowest_52_week_price")}
    print(f"  {label}: {len(rows)}종목", flush=True)
    return rows


def binance(usdkrw: float) -> dict:
    """바이낸스 USDT 마켓 (지역 제한 없는 공개 시세 미러). 가격·거래대금은 원/달러 환율로 원화 환산."""
    rows = {}
    for t in get("https://data-api.binance.vision/api/v3/ticker/24hr"):
        s = t["symbol"]
        if not s.endswith("USDT") or float(t["lastPrice"]) <= 0 or float(t["quoteVolume"]) <= 0:
            continue
        sym = s[:-4]
        if sym in STABLE or re.search(r"(UP|DOWN|BULL|BEAR)$", sym):
            continue
        usd = float(t["lastPrice"])
        rows[sym] = {"code": sym, "name": sym, "nameEn": None, "market": "BINANCE",
                     "price": round(usd * usdkrw, 4 if usd * usdkrw < 100 else 0), "priceUsd": usd,
                     "change": round(float(t["priceChangePercent"]), 2), "volume": float(t["volume"]),
                     "value": round(float(t["quoteVolume"]) * usdkrw), "high52w": None, "low52w": None}
    print(f"  바이낸스: {len(rows)}종목 (원/달러 {usdkrw:,.2f})", flush=True)
    return rows


def build_coins() -> dict:
    usdkrw = yahoo_quotes(["KRW=X"])["KRW=X"]["regularMarketPrice"]
    up = korean_exchange("https://api.upbit.com", "UPBIT")
    bt = korean_exchange("https://api.bithumb.com", "BITHUMB")
    bn = binance(usdkrw)
    gecko = []
    for page in (1, 2, 3):                            # 시가총액 상위 250
        gecko += get("https://api.coingecko.com/api/v3/coins/markets",
                     params={"vs_currency": "krw", "order": "market_cap_desc", "per_page": 100, "page": page})
        time.sleep(3)
    caps = {}
    for g in gecko[:250]:                             # 같은 기호가 여러 개면 시가총액 큰 것
        caps.setdefault(g["symbol"].upper(), g)
    names_ko = {k: v["name"] for src in (up, bt) for k, v in src.items()}
    stocks = []
    for ex in (up, bt, bn):
        for sym, r in ex.items():
            g = caps.get(sym)
            if r["market"] == "BINANCE":
                r["name"] = names_ko.get(sym) or (g["name"] if g else sym)
                r["nameEn"] = g["name"] if g else None
            r["marketCap"] = g["market_cap"] if g else None
            r["rank"] = g.get("market_cap_rank") if g else None
            r["symbol"] = f"{sym}-KRW"
            r["tags"] = [r["market"].lower()] + (["top"] if g else [])
            # 김치 프리미엄: 국내 거래소 원화 가격 ÷ (바이낸스 달러 가격 × 원/달러) - 1
            b = bn.get(sym)
            if r["market"] != "BINANCE" and b and b["priceUsd"]:
                r["premium"] = round((r["price"] / (b["priceUsd"] * usdkrw) - 1) * 100, 2)
            stocks.append(r)
    stocks.sort(key=lambda c: -(c.get("marketCap") or 0))
    return {"region": "COIN", "currency": "KRW", "usdkrw": usdkrw, "stocks": stocks}


def write(name: str, body: dict):
    if len(body["stocks"]) < 10:                      # 수집이 잘못되면 이전 파일을 지키게
        raise RuntimeError(f"{name}: 종목 수가 너무 적음({len(body['stocks'])})")
    body = {"generatedAt": datetime.now(KST).isoformat(timespec="seconds"), "count": len(body["stocks"]), **body}
    (OUT / name).write_text(json.dumps(body, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{name}: {body['count']}종목", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="us,jp,coin")
    a = ap.parse_args()
    jobs = {"us": ("us.json", build_us), "jp": ("jp.json", build_jp), "coin": ("coins.json", build_coins)}
    failed = []
    for key in a.only.split(","):
        name, fn = jobs[key]
        t0 = time.time()
        try:
            write(name, fn())
            print(f"  ({time.time() - t0:.0f}초)", flush=True)
        except Exception as e:                        # 하나가 실패해도 나머지는 만든다 (이전 파일은 그대로)
            failed.append(key)
            print(f"{name} 실패: {e!r}", flush=True)
    sys.exit(1 if failed and len(failed) == len(a.only.split(",")) else 0)
