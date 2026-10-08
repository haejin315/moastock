"""근거 검색: 질문 임베딩 → pgvector(종목·기간 필터) → 문서별로 고르게 추리기.

검색 결과마다 출처(수집 URL, 언론사 원문, 발행 시각, 문서 안 위치)를 그대로 달고 다닌다 -
답변의 [번호] 인용이 이 목록을 가리킨다.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

from config import (EVIDENCE_CHARS, EVIDENCE_MAX, KEYWORD_CANDIDATES, PG_DSN, RERANK_URL, SEARCH_CANDIDATES,
                    TABLE_CHARS, TABLE_MAX)

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


# 질문에서 표 항목명 후보 뽑기: 조사·어미를 떼고, 종목명·일반어는 뺀다
_PARTICLE = re.compile(r"(으로|에서|까지|부터|이랑|하고|이야|인가요|인가|이에요|예요|은|는|이|가|을|를|과|와|의|도|에|로|랑|야)$")
_STOP = {"알려줘", "알려주세요", "얼마야", "얼마", "얼마인가", "기준", "공시", "보고서", "반기보고서", "분기보고서",
         "사업보고서", "최근", "내용", "정리", "정리해줘", "요약", "요약해줘", "관련", "현재", "어떻게", "있어", "뭐야"}


def table_terms(query: str) -> list[str]:
    out = []
    for w in re.split(r"[\s,.?!·/()\[\]\"']+", query):
        w = _PARTICLE.sub("", w)
        if len(w) < 3 or w in _STOP or resolve_stocks(w) or re.fullmatch(r"[\d년월일말기분반]+", w):
            continue
        out.append(w)
    return out[:4]


def keyword_hits(query: str, vec, where: list[str], args: list, *, kind: str | None, limit: int) -> list:
    """항목명·핵심어가 그대로 들어간 청크 (종목 필터 안에서만 - 전체 300만 청크를 글자로 훑지 않게).
    행 모양은 벡터 검색 결과와 같게. 핵심어의 절반 이상이 들어간 것만."""
    terms = table_terms(query)
    if not terms:
        return []
    pats = [f"%{t}%" for t in terms]
    hits = " + ".join(["(content LIKE %s)::int"] * len(pats))
    cond = " AND ".join(where + ([f"kind = '{kind}'"] if kind else []) + ["content LIKE ANY(%s)"])
    sql = (f"SELECT chunk_id, doc_id, content, published_at, source_type, 1 - (embedding <=> %s) AS score, kind, "
           f"{hits} AS hits FROM chunk WHERE {cond} ORDER BY hits DESC, embedding <=> %s LIMIT {int(limit)}")
    rows = conn().execute(sql, (vec, *pats, *args, pats, vec)).fetchall()
    need = max(1, (len(terms) + 1) // 2)
    return [r[:7] for r in rows if r[7] >= need]


_http = None


def rerank(query: str, rows: list, titles: dict) -> list | None:
    """교차 인코더(bge-reranker-v2-m3, TEI)로 후보를 다시 매긴다. 서버가 없거나 실패하면 None."""
    global _http
    if not RERANK_URL or not rows:
        return None
    try:
        import requests
        _http = _http or requests.Session()
        texts = [f"{titles.get(r[1], '')}\n{r[2][:1200]}" for r in rows]
        res = _http.post(f"{RERANK_URL.rstrip('/')}/rerank", json={"query": query, "texts": texts, "truncate": True},
                         timeout=20)
        res.raise_for_status()
        order = sorted(res.json(), key=lambda x: -x["score"])
        return [rows[o["index"]][:5] + (float(o["score"]), rows[o["index"]][6]) for o in order]
    except Exception as e:
        print(f"[rerank] 실패, 벡터 순서 사용: {e!r}", flush=True)
        return None


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
    # 종목이 정해진 질문은 핵심어가 그대로 들어간 청크도 후보에 넣는다 (작은 임베딩 모델은 비슷한 숫자 표·
    # 항목명을 잘 못 가른다). 표는 따로 TABLE_MAX개를 맨 앞에 - 리랭커가 없을 때도 표 질문이 맞게.
    lexical = []
    if stocks and _local.has_kind:
        if source != "news":
            lexical += keyword_hits(query, vec, where, args, kind="table", limit=TABLE_MAX)
        lexical += keyword_hits(query, vec, where, args, kind=None, limit=KEYWORD_CANDIDATES)
    seen, merged = set(), []
    for r in lexical + rows:
        if r[0] not in seen:
            seen.add(r[0]); merged.append(r)
    rows = merged
    titles = {d[0]: d[1] for d in conn().execute("SELECT doc_id, title FROM doc WHERE doc_id = ANY(%s)",
                                                 (list({r[1] for r in rows}),)).fetchall()} if rows else {}
    rows = rerank(query, rows, titles) or rows

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
