"""1단계(b): 날짜별 네이버 뉴스 섹션 목록 수집 - 기간 전체 커버용.

  python -m moarag.collect_news_section [--sections 258,261] [--rps 10]

- 258 경제>증권: 전부 수집 (시황·종목 기사)
- 261 경제>산업/재계: 목록의 제목+요약문에 상장사명이 나오는 기사만 (기업 뉴스 위주로 거름)
날짜×섹션 단위로 끝까지 넘긴 뒤 section_day에 완료를 기록하므로 재실행 시 이어서 한다.
"""
from __future__ import annotations

import argparse
import asyncio
import html as htmllib
import json
import re
from datetime import datetime, timedelta

from .config import KST, SINCE_KST
from .http import Fetcher
from .metrics import Progress, Stage
from .rawdb import connect
from .stockmatch import StockMatcher

FIRST = "https://news.naver.com/breakingnews/section/101/{sid2}?date={date}"
MORE = "https://news.naver.com/section/template/SECTION_ARTICLE_LIST_FOR_LATEST"
FILTERED = {"261"}          # 상장사명이 보이는 기사만 남길 섹션

ITEM = re.compile(r'<li class="sa_item[\s\S]*?</li>')
LINK = re.compile(r'n\.news\.naver\.com/(?:mnews/)?article/(\d+)/(\d+)')
TITLE = re.compile(r'<strong class="sa_text_strong">([\s\S]*?)</strong>')
LEDE = re.compile(r'<div class="sa_text_lede">([\s\S]*?)</div>')
PRESS = re.compile(r'<div class="sa_text_press">([\s\S]*?)</div>')
CURSOR = re.compile(r'data-cursor="(\d+)"')
HAS_NEXT = re.compile(r'data-has-next="(\w+)"')


def text(m):
    return htmllib.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() if m else ""


def parse_items(html: str):
    out = []
    for block in ITEM.findall(html):
        m = LINK.search(block)
        if not m:
            continue
        out.append((m.group(1), m.group(2), text(TITLE.search(block)), text(LEDE.search(block)),
                    text(PRESS.search(block))))
    return out


async def crawl_day(f: Fetcher, sid2: str, date: str):
    _, _, html = await f.get(FIRST.format(sid2=sid2, date=date))
    items = parse_items(html)
    seen = {(o, a) for o, a, *_ in items}
    cur = CURSOR.search(html)
    nxt = HAS_NEXT.search(html)
    page = 1
    while cur and nxt and nxt.group(1) == "true":
        page += 1
        _, _, body = await f.get(MORE, as_json=True, params={
            "sid": "101", "sid2": sid2, "cluid": "", "pageNo": page, "date": date, "next": cur.group(1)})
        h = (body or {}).get("renderedComponent", {}).get("SECTION_ARTICLE_LIST_FOR_LATEST", "")
        new = [it for it in parse_items(h) if (it[0], it[1]) not in seen]
        if not new:
            break
        items += new
        seen |= {(o, a) for o, a, *_ in new}
        cur, nxt = CURSOR.search(h), HAS_NEXT.search(h)
    return items, page


async def main(sections: list[str], rps: float, workers: int, days_back: int | None = None,
               matcher: StockMatcher | None = None):
    """days_back 를 주면 (매일 증분) 오늘부터 그 날수만큼만, 완료 표시와 상관없이 다시 훑는다."""
    con = connect()
    matcher = matcher or StockMatcher()
    now = datetime.now(KST)
    first = (now - timedelta(days=days_back)).date() if days_back is not None else SINCE_KST.date()
    days = []
    d = now.date()
    while d >= first:
        days.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    done = {(s, dt) for s, dt in con.execute("SELECT sid2, date FROM section_day WHERE done_at IS NOT NULL")}
    today = now.strftime("%Y%m%d")
    todo = [(s, dt) for dt in days for s in sections
            if days_back is not None or (s, dt) not in done or dt == today]

    with Stage("news_section", sections=sections, days=len(days), todo=len(todo), rps=rps) as st:
        prog = Progress("섹션×날짜", total=len(todo))
        queue: asyncio.Queue = asyncio.Queue()
        for t in todo:
            queue.put_nowait(t)

        async with Fetcher(rps=rps, concurrency=workers) as f:
            async def worker():
                while True:
                    try:
                        sid2, date = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    try:
                        items, pages = await crawl_day(f, sid2, date)
                    except Exception as e:
                        print(f"  ! {sid2} {date} 실패: {e}", flush=True)
                        st.add(days_failed=1)
                        continue
                    rows, links = [], []
                    for oid, aid, title, lede, press in items:
                        m = matcher.match(title, lede)
                        if sid2 in FILTERED and not m:
                            continue
                        rows.append((oid, aid, f"https://n.news.naver.com/mnews/article/{oid}/{aid}",
                                     f"section:{sid2}", title, lede, press, date + "0000"))
                        links += [(code, oid, aid, date + "0000", how) for code, how in m.items()]
                    con.executemany(
                        "INSERT OR IGNORE INTO news(oid,aid,naver_url,list_source,list_title,list_lede,list_office,"
                        "list_datetime) VALUES(?,?,?,?,?,?,?,?)", rows)
                    con.executemany(
                        "INSERT OR IGNORE INTO news_link(code,oid,aid,listed_at,method) VALUES(?,?,?,?,?)", links)
                    con.execute(
                        "INSERT INTO section_day(sid2,date,pages,listed,kept,done_at) VALUES(?,?,?,?,?,?) "
                        "ON CONFLICT(sid2,date) DO UPDATE SET pages=excluded.pages, listed=excluded.listed, "
                        "kept=excluded.kept, done_at=excluded.done_at",
                        (sid2, date, pages, len(items), len(rows), datetime.now(KST).isoformat(timespec="seconds")))
                    con.commit()
                    st.add(days=1, pages=pages, listed=len(items), kept=len(rows), title_links=len(links))
                    prog.tick(kept=int(st.counts.get("kept", 0)))

            await asyncio.gather(*(worker() for _ in range(workers)))
            st.add(http_requests=f.stats["requests"], http_retries=f.stats["retries"], bytes=f.stats["bytes"])
        st.set(unique_articles_total=con.execute("SELECT COUNT(*) FROM news").fetchone()[0])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sections", default="258,261")
    ap.add_argument("--rps", type=float, default=10)
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    asyncio.run(main(a.sections.split(","), a.rps, a.workers))
