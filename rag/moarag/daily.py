"""매일 증분: 새 뉴스·공시 수집 → 정제·중복 제거 → 청킹 → 임베딩 → 운영 pgvector 에 추가 (GPU 서버에서 실행).

  MOARAG_DATA_DIR=/data/rag/daily MOARAG_EMBED_URL=http://embed:80 python -m moarag.daily [--days N]

- 기간: 운영 DB의 가장 최근 뉴스 날짜 하루 전부터 오늘까지 (최대 14일). --days 로 직접 정할 수 있다.
- 원천 저장소(raw.sqlite)는 서버의 MOARAG_DATA_DIR 에 따로 둔다 (처음 수집분은 로컬에 있다).
  처리한 기사·공시는 loaded_at 으로 표시해 다음 실행 때 다시 넣지 않는다.
- 중복 제거는 운영 DB 기준: 본문 해시가 같은 문서, 최근 며칠 뉴스와 거의 같은 기사(MinHash)는 새로 넣지 않고
  기존 문서의 dup_sources 에 출처만 덧붙인다.
- DART 원문은 하루 한도(키당 20,000건, 사이트와 함께 씀)를 생각해 한 번에 --dart-max 건까지만.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import uuid
from collections import defaultdict
from datetime import datetime, timedelta

import pyarrow as pa
import requests

from . import collect_dart, collect_news_section, crawl_news
from .config import DATA_DIR, KST
from .embed import make_encoder, passage_texts
from .metrics import Stage
from .process import (DART_SQL, NEWS_SQL, NS, NUM_PERM, PIPELINE_VERSION, chunk_worker, dart_doc,
                      doc_row, minhash_of, news_doc, norm_for_hash)
from .rawdb import connect, kv_set
from .stockmatch import StockMatcher
from .stores import PgVector

SNAPSHOT_URL = "https://moastock.co.kr/data/snapshot.json"
MAX_DAYS = 14
NEAR_DAYS = 4                       # 유사 중복 비교 대상: 운영 DB의 최근 N일 뉴스


def load_matcher() -> StockMatcher:
    """사이트의 최신 종목 목록으로 종목 사전을 만든다 (신규 상장 반영)."""
    path = DATA_DIR / "snapshot.json"
    try:
        r = requests.get(SNAPSHOT_URL, timeout=30)
        r.raise_for_status()
        path.write_bytes(r.content)
    except Exception as e:                              # 못 받으면 지난번 것으로
        print(f"  종목 목록 받기 실패({e!r}) - 이전 파일 사용", flush=True)
    return StockMatcher(json.loads(path.read_text(encoding="utf-8"))["stocks"])


def pick_days(pg: PgVector) -> int:
    last = pg.con.execute("SELECT max(published_at) FROM doc WHERE source_type = 'news'").fetchone()[0]
    if not last:
        return 2
    return max(1, min(MAX_DAYS, (datetime.now(KST) - last).days + 1))


def merge_source(pg: PgVector, keep_id: str, d: dict, kind: str):
    """중복으로 판정된 새 문서의 출처를 기존 문서에 덧붙인다 (출처가 사라지지 않게)."""
    src = {"doc_id": d["doc_id"], "publisher": d["publisher"], "url": d["url"], "original_url": d["original_url"],
           "published_at": d["published_at"], "match": kind}
    pg.con.execute("UPDATE doc SET dup_sources = COALESCE(dup_sources, '[]'::jsonb) || %s::jsonb WHERE doc_id = %s",
                   (json.dumps([src], ensure_ascii=False), keep_id))


def dedup(pg: PgVector, docs: list[dict], st) -> list[dict]:
    for d in docs:
        d["content_sha1"] = hashlib.sha1(norm_for_hash(d["text"]).encode()).hexdigest()
        d["dup_sources"] = []
    # 이미 운영 DB에 있는 문서(같은 doc_id)는 건너뛴다 - 처음 수집분과 기간이 겹칠 때
    have = {r[0] for r in pg.con.execute("SELECT doc_id FROM doc WHERE doc_id = ANY(%s)",
                                         ([d["doc_id"] for d in docs],)).fetchall()}
    docs = [d for d in docs if d["doc_id"] not in have]
    st.add(already_loaded=len(have))
    # 정확 중복: 운영 DB 또는 이번 묶음 안에서 본문 해시가 같은 문서
    known = {(r[1], r[2]): r[0] for r in pg.con.execute(
        "SELECT doc_id, source_type, content_sha1 FROM doc WHERE content_sha1 = ANY(%s)",
        ([d["content_sha1"] for d in docs],)).fetchall()}
    docs.sort(key=lambda d: (d["published_at"] or "", d["doc_id"]))
    out = []
    for d in docs:
        k = (d["source_type"], d["content_sha1"])
        if k in known:
            merge_source(pg, known[k], d, "exact"); st.add(exact_dups=1); continue
        known[k] = d["doc_id"]
        out.append(d)
    # 유사 중복(뉴스만): 운영 DB의 최근 뉴스 + 이번 묶음
    from datasketch import MinHashLSH
    lsh = MinHashLSH(threshold=0.85, num_perm=NUM_PERM)
    since = datetime.now(KST) - timedelta(days=NEAR_DAYS)
    for doc_id, body in pg.con.execute("SELECT doc_id, body FROM doc WHERE source_type = 'news' AND published_at >= %s",
                                       (since,)):
        lsh.insert(doc_id, minhash_of(body))
    kept = []
    for d in out:
        if d["source_type"] == "news":
            m = minhash_of(d["text"])
            hits = lsh.query(m)
            if hits:
                merge_source(pg, hits[0], d, "near"); st.add(near_dups=1); continue
            lsh.insert(d["doc_id"], m)
        kept.append(d)
    return kept


def to_chunks(d: dict) -> list[dict]:
    _, spans = chunk_worker((d["doc_id"], d["text"], d["source_type"]))
    seen, rows = set(), []
    for a, b, n, kind, t in spans:
        h = hashlib.sha1(norm_for_hash(t).encode()).digest()
        if h in seen:
            continue
        seen.add(h)
        rows.append(dict(chunk_id=str(uuid.uuid5(NS, f"{d['doc_id']}#{len(rows)}")), doc_id=d["doc_id"],
                         chunk_index=len(rows), text=t, title=d["title"], char_start=a, char_end=b, n_tokens=n,
                         source_type=d["source_type"], published_at=d["published_at"],
                         stock_codes=d["stock_codes"], kind=kind))
    return rows


async def collect(days: int, dart_max: int, matcher: StockMatcher):
    since = datetime.now(KST) - timedelta(days=days)
    await collect_news_section.main(["258", "261"], rps=5, workers=3, days_back=days, matcher=matcher)
    await crawl_news.main(limit=8000, rps=6, workers=6)
    try:
        await collect_dart.collect_list(since=since)
        await collect_dart.collect_docs(limit=dart_max, rps=4)
    except collect_dart.QuotaExceeded as e:             # 한도가 차도 뉴스·이미 받은 공시는 넣는다
        print(f"  DART 한도: {e}", flush=True)


def main(days: int | None, dart_max: int, batch: int):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    pg = PgVector()
    days = days or pick_days(pg)
    matcher = load_matcher()
    with Stage("daily", days=days, dart_max=dart_max) as st:
        asyncio.run(collect(days, dart_max, matcher))

        con = connect()
        links: dict[str, dict[str, str]] = defaultdict(dict)
        for code, oid, aid, how in con.execute("SELECT code, oid, aid, method FROM news_link"):
            links[f"news:{oid}-{aid}"][code] = how
        since_iso = (datetime.now(KST) - timedelta(days=days + 1)).isoformat()
        docs, news_keys, dart_keys = [], [], []
        for row in con.execute(NEWS_SQL + " WHERE status='ok' AND loaded_at IS NULL"):
            news_keys.append((row[0], row[1]))
            d, why = news_doc(row, links, matcher, since_iso)
            if d:
                docs.append(d)
            else:
                st.add(**{f"news_{why}": 1})
        for row in con.execute(DART_SQL + " WHERE status='ok' AND loaded_at IS NULL"):
            dart_keys.append(row[0])
            d = dart_doc(row)
            if d:
                docs.append(d)
            else:
                st.add(dart_too_short=1)
        st.add(candidates=len(docs))

        docs = dedup(pg, docs, st)
        enc = make_encoder()
        added = 0
        for i in range(0, len(docs), 200):                # 200문서씩: 문서 → 청크 → 임베딩 → 한 트랜잭션으로 넣기
            part = docs[i:i + 200]
            chunks = [c for d in part for c in to_chunks(d)]
            vecs = enc.encode(passage_texts(pa.Table.from_pylist(chunks)), batch) if chunks else []
            with pg.con.transaction():
                pg.load_docs([doc_row(d) | {"text": d["text"]} for d in part])
                if chunks:
                    pg.load(chunks, vecs)
            added += len(part)
            st.add(docs=len(part), chunks=len(chunks), **{f"chunks_{k}": sum(c["kind"] == k for c in chunks)
                                                          for k in ("text", "table")})
            print(f"  추가 {added:,}/{len(docs):,} 문서", flush=True)

        now = datetime.now(KST).isoformat(timespec="seconds")
        con.executemany("UPDATE news SET loaded_at=? WHERE oid=? AND aid=?", [(now, o, a) for o, a in news_keys])
        con.executemany("UPDATE dart_filing SET loaded_at=?, applied_v=2 WHERE rcept_no=?", [(now, r) for r in dart_keys])
        con.commit()
        kv_set(con, "daily_last", now)
        st.set(pipeline_version=PIPELINE_VERSION,
               total_chunks=pg.con.execute("SELECT count(*) FROM chunk").fetchone()[0])
    print(f"완료: 새 문서 {added:,}건", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, help="오늘부터 며칠 전까지 (기본: 운영 DB 최근 뉴스 기준 자동)")
    ap.add_argument("--dart-max", type=int, default=3000)
    ap.add_argument("--batch", type=int, default=64)
    a = ap.parse_args()
    main(a.days, a.dart_max, a.batch)
