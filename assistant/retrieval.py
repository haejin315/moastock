"""근거 검색: 질문 임베딩 → pgvector(종목·기간 필터) → 문서별로 고르게 추리기.

검색 결과마다 출처(수집 URL, 언론사 원문, 발행 시각, 문서 안 위치)를 그대로 달고 다닌다 -
답변의 [번호] 인용이 이 목록을 가리킨다.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

from config import EVIDENCE_CHARS, EVIDENCE_MAX, PG_DSN, SEARCH_CANDIDATES, TABLE_CHARS, TABLE_MAX

KST = timezone(timedelta(hours=9))
_local = threading.local()
_encoder = None
_matcher = None
_lock = threading.Lock()


def encoder():
    global _encoder
    with _lock:
        if _encoder is None:
            from moarag.embed import make_encoder     # MOARAG_EMBED_URL 이 있으면 GPU 임베딩 서버
            _encoder = make_encoder(threads=4)
    return _encoder


def matcher():
    """종목 사전 = 스냅샷 종목명 + DART 공시 회사명(예: 'LS ELECTRIC' ↔ '엘에스일렉트릭')"""
    global _matcher
    with _lock:
        if _matcher is None:
            import json
            from config import ROOT
            from moarag.stockmatch import StockMatcher
            stocks = json.loads((ROOT / "public" / "data" / "snapshot.json").read_text(encoding="utf-8"))["stocks"]
            known = {(s["name"], s["code"]) for s in stocks}
            rows = conn().execute(
                "SELECT DISTINCT corp_name, stock_codes[1] FROM doc "
                "WHERE source_type = 'dart' AND corp_name IS NOT NULL AND cardinality(stock_codes) > 0").fetchall()
            extra = [{"name": n, "code": c} for n, c in rows if (n, c) not in known]
            _matcher = StockMatcher([{"name": n, "code": c} for n, c in known] + extra)
    return _matcher


def conn():
    c = getattr(_local, "conn", None)
    if c is None or c.closed:
        c = psycopg.connect(PG_DSN, autocommit=True)
        register_vector(c)
        c.execute("SET hnsw.ef_search = 100")
        # 청크 종류(text|table) 열이 있는지 (표 청크 도입 전 DB와도 동작하게)
        _local.has_kind = bool(c.execute("SELECT 1 FROM information_schema.columns "
                                    "WHERE table_name = 'chunk' AND column_name = 'kind'").fetchone())
        c.execute("SET hnsw.iterative_scan = strict_order")
        _local.conn = c
    return c


def resolve_stocks(text: str) -> list[str]:
    """질문에 나온 회사명·종목코드 → 종목코드 (사전 매칭, 모델 없이)"""
    return list(matcher().match(text, text).keys())


@dataclass
class Evidence:
    n: int
    chunk_id: str
    doc_id: str
    text: str
    score: float
    title: str
    publisher: str | None
    published_at: datetime | None
    url: str
    original_url: str | None
    source_type: str
    report_nm: str | None
    kind: str = "text"               # text(문단) | table(공시 표)

    def citation(self) -> dict:
        return {"n": self.n, "title": self.title, "publisher": self.publisher,
                "published_at": self.published_at.astimezone(KST).strftime("%Y-%m-%d") if self.published_at else None,
                "url": self.url, "original_url": self.original_url, "type": self.source_type,
                "report": self.report_nm, "kind": self.kind}


def clip(content: str, kind: str) -> str:
    """근거 길이 제한. 표는 행 중간에서 자르지 않는다."""
    if kind != "table":
        return content[:EVIDENCE_CHARS]
    if len(content) <= TABLE_CHARS:
        return content
    cut = content.rfind("\n", 0, TABLE_CHARS)
    return content[:cut if cut > 0 else TABLE_CHARS]


def search(query: str, *, stocks: list[str] | None = None, since_days: int | None = None,
           period: tuple[datetime, datetime] | None = None,
           source: str | None = None, limit: int = EVIDENCE_MAX) -> list[Evidence]:
    vec = encoder().encode([f"query: {query}"], batch=1)[0].astype(np.float32)
    where, args = [], []
    if stocks:
        where.append("stock_codes && %s::text[]"); args.append(stocks)
    if period:                       # "8월"처럼 특정 기간을 말한 경우
        where.append("published_at >= %s AND published_at < %s"); args += [period[0], period[1]]
    elif since_days:
        where.append("published_at >= %s"); args.append(datetime.now(KST) - timedelta(days=since_days))
    if source in ("news", "dart"):
        where.append("source_type = %s"); args.append(source)
    conn()
    kind_col = "kind" if _local.has_kind else "'text'"
    sql = (f"SELECT chunk_id, doc_id, content, published_at, source_type, 1 - (embedding <=> %s) AS score, "
           f"{kind_col} AS kind "
           f"FROM chunk {'WHERE ' + ' AND '.join(where) if where else ''} "
           f"ORDER BY embedding <=> %s LIMIT {SEARCH_CANDIDATES}")
    rows = conn().execute(sql, (vec, *args, vec)).fetchall()

    # 문서당 최대 2개 - 한 기사에 근거가 쏠리지 않게. 공시 표 청크는 전체에서 TABLE_MAX개까지
    picked, per_doc, tables = [], {}, 0
    for r in rows:
        if per_doc.get(r[1], 0) >= 2 or (r[6] == "table" and tables >= TABLE_MAX):
            continue
        per_doc[r[1]] = per_doc.get(r[1], 0) + 1
        tables += r[6] == "table"
        picked.append(r)
        if len(picked) >= limit:
            break
    if not picked:
        return []
    docs = {d[0]: d for d in conn().execute(
        "SELECT doc_id, title, publisher, url, original_url, report_nm FROM doc WHERE doc_id = ANY(%s)",
        ([r[1] for r in picked],)).fetchall()}
    out = []
    for i, (cid, did, content, pub, stype, score, kind) in enumerate(picked, 1):
        d = docs.get(did, (did, did, None, "", None, None))
        out.append(Evidence(n=i, chunk_id=str(cid), doc_id=did, text=clip(content, kind), score=float(score),
                            title=d[1], publisher=d[2], published_at=pub, url=d[3], original_url=d[4],
                            source_type=stype, report_nm=d[5], kind=kind))
    return out
