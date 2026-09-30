"""전 종목 일일 스냅샷 생성기.

네이버 모바일 증권 API에서 KOSPI/KOSDAQ 전 종목의 시세·시총과
펀더멘털(PER/PBR/EPS/BPS/배당수익률/외국인비율/52주 고저/업종)을 수집해
public/data/snapshot.json 으로 저장한다.

- 장마감 후 하루 1회 실행하면 충분한 데이터만 담는다 (장중 시세는
  /api/screener 프록시가 실시간으로 덮어쓴다).
- GitHub Actions 평일 크론에서 실행되고, 로컬에서도 그대로 돈다:
    python scripts/build_snapshot.py [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = "https://m.stock.naver.com/api"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (moastock snapshot; +https://github.com/haejin315/moastock)",
    "Accept": "application/json",
}
OUT = Path(__file__).resolve().parents[1] / "public" / "data" / "snapshot.json"
KST = timezone(timedelta(hours=9))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def get_json(url: str, retries: int = 3) -> dict:
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except Exception as exc:  # noqa: BLE001 - 재시도 후 상위에서 처리
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{url}: {last}")


_NUM = re.compile(r"-?[\d,]+(?:\.\d+)?")


def num(text) -> float | None:
    """'12.04배', '1,668원', '46.47%', 'N/A' → float 또는 None."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = _NUM.search(str(text))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def list_market(market: str) -> list[dict]:
    """시가총액 목록 API - 전 종목의 코드/이름/시세/시총."""
    out: list[dict] = []
    page = 1
    while True:
        body = get_json(f"{BASE}/stocks/marketValue/{market}?page={page}&pageSize=100")
        stocks = body.get("stocks") or []
        for s in stocks:
            if s.get("stockEndType") != "stock":
                continue  # ETF/ETN/리츠 등은 별도 트랙 - 스크리너 1차 범위는 보통주
            out.append(
                {
                    "code": s["itemCode"],
                    "name": s["stockName"],
                    "market": market,
                    "price": num(s.get("closePrice")),
                    "change": num(s.get("fluctuationsRatio")),
                    "volume": num(s.get("accumulatedTradingVolume")),
                    # 단위 통일(원): 거래대금은 백만원, 시가총액은 억원 단위로 온다
                    # (marketValueHangeul "1,569조..."와 대조해 확인)
                    "value": (num(s.get("accumulatedTradingValue")) or 0) * 1_000_000,
                    "marketCap": (num(s.get("marketValue")) or 0) * 100_000_000,
                }
            )
        total = int(body.get("totalCount") or 0)
        if page * 100 >= total or not stocks:
            break
        page += 1
    return out


TOTAL_FIELDS = {
    "per": "per",
    "pbr": "pbr",
    "eps": "eps",
    "bps": "bps",
    "dividendYieldRatio": "dividendYield",
    "foreignRate": "foreignRate",
    "highPriceOf52Weeks": "high52w",
    "lowPriceOf52Weeks": "low52w",
}


def enrich(stock: dict) -> dict:
    body = get_json(f"{BASE}/stock/{stock['code']}/integration")
    infos = {t.get("code"): t.get("value") for t in body.get("totalInfos", [])}
    for src, dst in TOTAL_FIELDS.items():
        stock[dst] = num(infos.get(src))
    stock["industryCode"] = str(body.get("industryCode") or "")
    return stock


def industry_names(codes: set[str]) -> dict[str, str]:
    """업종코드 → 업종명 (예: 278 → 반도체와반도체장비)."""
    names: dict[str, str] = {}
    for code in sorted(c for c in codes if c):
        try:
            body = get_json(f"{BASE}/stocks/industry/{code}?page=1&pageSize=1")
            names[code] = str((body.get("groupInfo") or {}).get("name") or "")
        except Exception:  # noqa: BLE001 - 이름 없는 업종은 코드로 표기
            names[code] = ""
        time.sleep(0.05)
    return names


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="테스트용: 종목 수 제한")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    stocks: list[dict] = []
    for market in ("KOSPI", "KOSDAQ"):
        part = list_market(market)
        print(f"{market}: {len(part)} 종목")
        stocks.extend(part)
    if args.limit:
        stocks = stocks[: args.limit]

    done = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(enrich, s): s for s in stocks}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001 - 일부 실패는 스킵하고 계속
                failed += 1
                futures[future]["industry"] = futures[future].get("industry", "")
            done += 1
            if done % 200 == 0:
                print(f"  {done}/{len(stocks)} (실패 {failed})")

    names = industry_names({s.get("industryCode", "") for s in stocks})
    for s in stocks:
        s["industry"] = names.get(s.pop("industryCode", ""), "") or ""
    print(f"업종 {len(names)}개 매핑")

    snapshot = {
        "generatedAt": datetime.now(KST).isoformat(timespec="seconds"),
        "source": "m.stock.naver.com",
        "count": len(stocks),
        "stocks": stocks,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    size_kb = OUT.stat().st_size // 1024
    print(f"저장: {OUT} ({len(stocks)} 종목, {size_kb} KB, 실패 {failed})")
    return 0 if failed < len(stocks) * 0.05 else 1


if __name__ == "__main__":
    raise SystemExit(main())
