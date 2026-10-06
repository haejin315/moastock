"""1단계(a): 전 종목 뉴스 목록 수집 (네이버증권 종목뉴스 API) - 종목 태그 확보용.

한계: 이 API는 종목당 최근 100페이지(2,000건)까지만 준다. 대형주는 며칠치에 불과하므로
기간 전체는 collect_news_section(날짜별 섹션 목록)이 담당하고, 여기서는 네이버가 붙인
종목 태그(news_link.method='naver_tag')를 최대한 확보한다.

종목마다 최신순으로 페이지를 넘기며 SINCE_KST 이전 기사가 나오면 멈춘다.
기사 본문은 받지 않고 (oid, aid)와 종목 매핑만 쌓는다 → 2단계(crawl_news)가 본문 수집.

  python -m moarag.collect_news_index [--limit 50] [--rps 12]

주의: pageSize는 20을 넘기면 페이지 경계가 어긋난다(서버가 20 단위로 오프셋 계산).
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime

from .config import KST, SINCE_KST, SNAPSHOT
from .http import Fetcher
from .metrics import Progress, Stage
from .rawdb import connect

API = "https://m.stock.naver.com/api/news/stock/{code}"
PAGE_SIZE = 20
MAX_PAGES = 100     # API 상한 (101페이지부터 HTTP 400)


async def index_stock(f: Fetcher, code: str, since: str):
    links, seen = [], set()
    page, pages = 1, 0
    while page <= MAX_PAGES:
        _, _, body = await f.get(API.format(code=code), params={"pageSize": PAGE_SIZE, "page": page}, as_json=True)
        pages += 1
        items = [it for g in (body or []) for it in g.get("items", [])]
        if not items:
            break
        older = 0
        for it in items:
            dt = it.get("datetime", "")
            if dt < since:
                older += 1
                continue
            key = (it["officeId"], it["articleId"])
            if key in seen:
                continue
            seen.add(key)
            links.append((code, it["officeId"], it["articleId"], dt,
                          it.get("titleFull") or it.get("title") or "", it.get("officeName") or ""))
        # 목록은 최신순 - 한 페이지가 통째로 기간 밖이면 끝
        if older == len(items):
            break
        page += 1
    return links, pages


async def main(limit: int | None, rps: float, workers: int):
    con = connect()
    stocks = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["stocks"]
    stocks.sort(key=lambda s: -(s.get("marketCap") or 0))
    con.executemany("INSERT OR IGNORE INTO stock(code,name,market) VALUES(?,?,?)",
                    [(s["code"], s["name"], s["market"]) for s in stocks])
    con.commit()
    todo = [r[0] for r in con.execute(
        "SELECT code FROM stock WHERE news_indexed_at IS NULL")]
    order = {s["code"]: i for i, s in enumerate(stocks)}
    todo.sort(key=lambda c: order.get(c, 1e9))
    if limit:
        todo = todo[:limit]
    since = SINCE_KST.strftime("%Y%m%d%H%M")

    with Stage("news_index", since=since, stocks=len(todo), rps=rps) as st:
        prog = Progress("종목", total=len(todo))
        queue: asyncio.Queue = asyncio.Queue()
        for c in todo:
            queue.put_nowait(c)

        async with Fetcher(rps=rps, concurrency=workers) as f:
            async def worker():
                while True:
                    try:
                        code = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    try:
                        links, pages = await index_stock(f, code, since)
                    except Exception as e:  # 다음 실행에서 재시도
                        print(f"  ! {code} 실패: {e}", flush=True)
                        st.add(stocks_failed=1)
                        continue
                    con.executemany(
                        "INSERT OR IGNORE INTO news_link(code,oid,aid,listed_at) VALUES(?,?,?,?)",
                        [l[:4] for l in links])
                    con.executemany(
                        "INSERT OR IGNORE INTO news(oid,aid,naver_url,list_source,list_title,list_office,list_datetime) "
                        "VALUES(?,?,?,'stock_api',?,?,?)",
                        [(o, a, f"https://n.news.naver.com/mnews/article/{o}/{a}", t, off, dt)
                         for _, o, a, dt, t, off in links])
                    con.execute("UPDATE stock SET news_indexed_at=?, news_links=? WHERE code=?",
                                (datetime.now(KST).isoformat(timespec="seconds"), len(links), code))
                    con.commit()
                    st.add(stocks=1, pages=pages, links=len(links))
                    prog.tick(links=int(st.counts.get("links", 0)))

            await asyncio.gather(*(worker() for _ in range(workers)))
            st.add(http_requests=f.stats["requests"], http_retries=f.stats["retries"], bytes=f.stats["bytes"])
        uniq = con.execute("SELECT COUNT(*) FROM news").fetchone()[0]
        st.set(unique_articles_total=uniq)
        print(f"  고유 기사 누적 {uniq:,}건", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--rps", type=float, default=12)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    asyncio.run(main(a.limit, a.rps, a.workers))
