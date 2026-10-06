"""2단계: 기사 원문 수집 (n.news.naver.com 기사 페이지).

1단계에서 쌓인 status='pending' 기사를 받아 본문·제목·언론사·기자·발행/수정 시각·
언론사 원문 링크·섹션을 추출해 저장한다. 본문은 여기서 '추출'만 하고 정제는 process 단계에서.

  python -m moarag.crawl_news [--limit 1000] [--rps 15]

스포츠/연예 기사는 별도 도메인(JS 렌더링)으로 리다이렉트되어 본문이 없을 수 있다 → status='empty'.
"""
from __future__ import annotations

import argparse
import asyncio
import re
from datetime import datetime

from selectolax.parser import HTMLParser

from .config import KST
from .http import Fetcher
from .metrics import Progress, Stage
from .rawdb import connect

BR = re.compile(r"<br\s*/?>", re.I)
DROP = ["script", "style", ".end_photo_org", "em.img_desc", ".vod_player_wrap", ".media_end_summary",
        ".artical-btm", "#dic_area .byline"]


def parse_article(html: str) -> dict:
    tree = HTMLParser(html)
    out: dict = {}

    node = tree.css_first("#dic_area") or tree.css_first("#newsct_article") or tree.css_first("#articeBody")
    if node is not None:
        for sel in DROP:
            for n in node.css(sel):
                n.decompose()
        inner = HTMLParser(BR.sub("\n", node.html or ""))
        out["body"] = (inner.body or inner.root).text(separator="", strip=False) if inner.root else ""
    else:
        out["body"] = ""

    def meta(prop):
        m = tree.css_first(f'meta[property="{prop}"]')
        return m.attributes.get("content") if m else None

    t = tree.css_first("#title_area")
    out["title"] = (t.text(strip=True) if t else None) or meta("og:title") or ""
    author = meta("og:article:author") or ""
    logo = tree.css_first(".media_end_head_top_logo img")
    out["press"] = (logo.attributes.get("alt") if logo else None) or author.split("|")[0].strip()
    out["reporter"] = ", ".join(
        dict.fromkeys(n.text(strip=True) for n in tree.css(".media_end_head_journalist_name"))) or None
    d = tree.css_first("._ARTICLE_DATE_TIME")
    out["published_at"] = d.attributes.get("data-date-time") if d else None
    m = tree.css_first("._ARTICLE_MODIFY_DATE_TIME")
    out["modified_at"] = m.attributes.get("data-modify-date-time") if m else None
    o = tree.css_first("a.media_end_head_origin_link")
    out["original_url"] = o.attributes.get("href") if o else None
    out["section"] = ",".join(n.text(strip=True) for n in tree.css(".media_end_categorize_item")) or None
    return out


async def main(limit: int | None, rps: float, workers: int):
    con = connect()
    sql = "SELECT oid, aid, naver_url FROM news WHERE status='pending' ORDER BY list_datetime DESC"
    rows = con.execute(sql + (f" LIMIT {int(limit)}" if limit else "")).fetchall()

    with Stage("crawl_news", articles=len(rows), rps=rps) as st:
        prog = Progress("기사", total=len(rows))
        queue: asyncio.Queue = asyncio.Queue()
        for r in rows:
            queue.put_nowait(r)
        pending_writes: list = []

        def flush():
            con.executemany(
                "UPDATE news SET status=?, http_status=?, final_url=?, error=?, fetched_at=?, title=?, body=?, "
                "press=?, reporter=?, published_at=?, modified_at=?, section=?, original_url=? "
                "WHERE oid=? AND aid=?", pending_writes)
            con.commit()
            pending_writes.clear()

        async with Fetcher(rps=rps, concurrency=workers) as f:
            async def worker():
                while True:
                    try:
                        oid, aid, url = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    now = datetime.now(KST).isoformat(timespec="seconds")
                    try:
                        status, final, html = await f.get(url)
                        p = parse_article(html) if status == 200 else {}
                        body = (p.get("body") or "").strip()
                        state = "ok" if body else ("empty" if status == 200 else "failed")
                        pending_writes.append((
                            state, status, final, None if state != "failed" else f"HTTP {status}", now,
                            p.get("title"), body or None, p.get("press"), p.get("reporter"),
                            p.get("published_at"), p.get("modified_at"), p.get("section"),
                            p.get("original_url"), oid, aid))
                        st.add(**{state: 1, "body_chars": len(body)})
                    except Exception as e:
                        pending_writes.append(("failed", None, None, str(e)[:300], now,
                                               None, None, None, None, None, None, None, None, oid, aid))
                        st.add(failed=1)
                    if len(pending_writes) >= 200:
                        flush()
                    prog.tick(ok=int(st.counts.get("ok", 0)), failed=int(st.counts.get("failed", 0)))

            await asyncio.gather(*(worker() for _ in range(workers)))
            flush()
            st.add(http_requests=f.stats["requests"], http_retries=f.stats["retries"], bytes=f.stats["bytes"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--rps", type=float, default=15)
    ap.add_argument("--workers", type=int, default=16)
    a = ap.parse_args()
    asyncio.run(main(a.limit, a.rps, a.workers))
