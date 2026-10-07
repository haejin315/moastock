"""벡터 저장소 어댑터: pgvector(PostgreSQL) · Qdrant · OpenSearch.

세 저장소 모두 같은 데이터(chunks.parquet + embeddings), 같은 HNSW 파라미터
(m=HNSW_M, ef_construction=HNSW_EF_CONSTRUCTION), 같은 거리(코사인)로 만든다.
각 어댑터는 create → load(배치) → finalize(인덱스 빌드/최적화) → search 를 제공한다.

필터 검색 시나리오: "특정 종목 + 특정 날짜 이후" (stock_codes ∋ code AND published_at >= since)
"""
from __future__ import annotations

import subprocess
import uuid
from datetime import datetime

from .config import (COLLECTION, EMBED_DIM, HNSW_EF_CONSTRUCTION, HNSW_M, OPENSEARCH_URL, PG_DSN,
                     QDRANT_URL)

# 저장소에 함께 넣는 청크 메타데이터 (출처 추적용)
META_COLS = ["chunk_id", "doc_id", "chunk_index", "n_chunks", "text", "char_start", "char_end", "n_tokens",
             "source_type", "title", "publisher", "author", "published_at", "url", "original_url",
             "stock_codes", "stock_names", "stock_match", "corp_name", "report_nm", "rcept_no", "section",
             "n_dup_sources", "crawled_at", "kind"]


def meta_cols(pf) -> list[str]:
    """parquet 에 있는 메타 열만 (예전 청크 파일에는 kind 가 없다)"""
    names = set(pf.schema_arrow.names)
    return [c for c in META_COLS if c in names]


def _ts(s):
    return datetime.fromisoformat(s) if s else None


# ---- pgvector -----------------------------------------------------------------

class PgVector:
    name = "pgvector"

    def __init__(self):
        import psycopg
        from pgvector.psycopg import register_vector
        self.con = psycopg.connect(PG_DSN, autocommit=True)
        self.con.execute("CREATE EXTENSION IF NOT EXISTS vector")
        register_vector(self.con)

    def create(self):
        # 문서(원문·출처)와 청크(검색 단위)를 분리한 정규화 스키마. 필터에 쓰는 컬럼만 청크에 비정규화.
        self.con.execute("""
        DROP TABLE IF EXISTS chunk; DROP TABLE IF EXISTS doc;
        CREATE TABLE doc (
          doc_id        text PRIMARY KEY,
          source_type   text NOT NULL CHECK (source_type IN ('news','dart')),
          title         text NOT NULL,
          publisher     text,
          author        text,
          published_at  timestamptz,
          modified_at   timestamptz,
          url           text NOT NULL,          -- 수집 출처 (네이버 기사 / DART 뷰어)
          original_url  text,                   -- 언론사 원문
          section       text,
          stock_codes   text[] NOT NULL DEFAULT '{}',
          stock_match   jsonb,                  -- {종목코드: 매핑방식}
          corp_name     text, report_nm text, rcept_no text,
          content_sha1  text NOT NULL,
          dup_sources   jsonb,                  -- 중복 제거로 합쳐진 다른 출처들
          crawled_at    timestamptz,
          body          text NOT NULL           -- 정제 전문: 청크 오프셋으로 인용 검증
        );
        CREATE TABLE chunk (
          chunk_id      uuid PRIMARY KEY,
          doc_id        text NOT NULL REFERENCES doc(doc_id) ON DELETE CASCADE,
          chunk_index   int  NOT NULL,
          char_start    int  NOT NULL,
          char_end      int  NOT NULL,
          n_tokens      int,
          content       text NOT NULL,
          source_type   text NOT NULL,
          published_at  timestamptz,
          stock_codes   text[] NOT NULL DEFAULT '{}',
          embedding     vector(%d) NOT NULL,
          kind          text NOT NULL DEFAULT 'text'   -- text(문단) | table(공시 표)
        );
        """ % EMBED_DIM)

    def load_docs(self, docs):
        cols = ["doc_id", "source_type", "title", "publisher", "author", "published_at", "modified_at", "url",
                "original_url", "section", "stock_codes", "stock_match", "corp_name", "report_nm", "rcept_no",
                "content_sha1", "dup_sources", "crawled_at", "body"]
        with self.con.cursor().copy(f"COPY doc ({','.join(cols)}) FROM STDIN") as cp:
            for d in docs:
                cp.write_row([d["doc_id"], d["source_type"], d["title"] or "", d["publisher"], d["author"],
                              _ts(d["published_at"]), _ts(d["modified_at"]), d["url"], d["original_url"],
                              d["section"], d["stock_codes"] or [], d["stock_match"], d["corp_name"],
                              d["report_nm"], d["rcept_no"], d["content_sha1"], d["dup_sources"],
                              _ts(d["crawled_at"]), d["text"]])

    def load(self, rows, vecs):
        with self.con.cursor().copy(
                "COPY chunk (chunk_id,doc_id,chunk_index,char_start,char_end,n_tokens,content,source_type,"
                "published_at,stock_codes,embedding,kind) FROM STDIN WITH (FORMAT BINARY)") as cp:
            cp.set_types(["uuid", "text", "int4", "int4", "int4", "int4", "text", "text", "timestamptz",
                          "text[]", "vector", "text"])
            for r, v in zip(rows, vecs):
                cp.write_row([uuid.UUID(r["chunk_id"]), r["doc_id"], r["chunk_index"], r["char_start"], r["char_end"],
                              r["n_tokens"], r["text"], r["source_type"], _ts(r["published_at"]),
                              r["stock_codes"] or [], v, r.get("kind") or "text"])

    def finalize(self):
        t = {}
        import time
        for label, sql in [
            ("hnsw", f"CREATE INDEX chunk_embedding_hnsw ON chunk USING hnsw (embedding vector_cosine_ops) "
                     f"WITH (m={HNSW_M}, ef_construction={HNSW_EF_CONSTRUCTION})"),
            ("gin_stock", "CREATE INDEX chunk_stock_codes ON chunk USING gin (stock_codes)"),
            ("btree_date", "CREATE INDEX chunk_published_at ON chunk (published_at)"),
            ("btree_doc", "CREATE INDEX chunk_doc_id ON chunk (doc_id)"),
            ("doc_gin_stock", "CREATE INDEX doc_stock_codes ON doc USING gin (stock_codes)"),
            ("analyze", "ANALYZE doc; ANALYZE chunk"),
        ]:
            t0 = time.perf_counter()
            self.con.execute(sql)
            t[label] = round(time.perf_counter() - t0, 2)
        return t

    def size_bytes(self):
        r = self.con.execute("SELECT pg_total_relation_size('chunk') + pg_total_relation_size('doc'), "
                             "pg_relation_size('chunk_embedding_hnsw')").fetchone()
        return {"total": r[0], "vector_index": r[1]}

    def count(self):
        return self.con.execute("SELECT count(*) FROM chunk").fetchone()[0]

    def set_ef(self, ef):
        self.ef = int(ef)
        self.con.execute(f"SET hnsw.ef_search = {int(ef)}")
        # 필터로 후보가 모자라면 인덱스를 더 훑는다 (pgvector 0.8 iterative scan)
        self.con.execute("SET hnsw.iterative_scan = strict_order")

    def search(self, vec, k=10, flt=None):
        if flt:
            sql = ("SELECT chunk_id FROM chunk WHERE stock_codes @> ARRAY[%s]::text[] AND published_at >= %s "
                   "ORDER BY embedding <=> %s LIMIT %s")
            args = (flt["stock_code"], _ts(flt["since"]), vec, k)
        else:
            sql, args = "SELECT chunk_id FROM chunk ORDER BY embedding <=> %s LIMIT %s", (vec, k)
        return [str(r[0]) for r in self.con.execute(sql, args).fetchall()]

    def clone(self):
        # 동시성 측정용 새 연결 - 세션 설정(ef_search)도 같이 옮겨야 같은 조건이 된다
        c = PgVector()
        if getattr(self, "ef", None):
            c.set_ef(self.ef)
        return c


# ---- Qdrant -------------------------------------------------------------------

class Qdrant:
    name = "qdrant"

    def __init__(self):
        from qdrant_client import QdrantClient
        from concurrent.futures import ThreadPoolExecutor
        self.c = QdrantClient(url=QDRANT_URL, timeout=300, prefer_grpc=True)   # gRPC: 공식 권장 고속 경로
        self.ef = 100
        self.pool = ThreadPoolExecutor(4)

    def create(self):
        from qdrant_client import models as m
        if self.c.collection_exists(COLLECTION):
            self.c.delete_collection(COLLECTION)
        self.c.create_collection(
            COLLECTION,
            vectors_config=m.VectorParams(size=EMBED_DIM, distance=m.Distance.COSINE, on_disk=False),
            hnsw_config=m.HnswConfigDiff(m=HNSW_M, ef_construct=HNSW_EF_CONSTRUCTION),
            # 대량 적재 중에는 인덱싱을 끄고(threshold=0) 끝난 뒤 한 번에 만든다 - 공식 권장 방식
            optimizers_config=m.OptimizersConfigDiff(indexing_threshold=0),
        )
        for field, schema in [("stock_codes", m.PayloadSchemaType.KEYWORD),
                              ("source_type", m.PayloadSchemaType.KEYWORD),
                              ("doc_id", m.PayloadSchemaType.KEYWORD),
                              ("published_at", m.PayloadSchemaType.DATETIME)]:
            self.c.create_payload_index(COLLECTION, field, field_schema=schema)

    def load_docs(self, docs):
        pass    # 문서 전문은 Qdrant에 두지 않는다 (청크 payload에 출처 메타데이터 포함)

    def load(self, rows, vecs):
        from qdrant_client import models as m
        # upload_points(parallel=N)는 호출마다 프로세스를 새로 띄워(Windows spawn) 느리므로
        # 512개 단위 upsert를 스레드 4개로 병렬 전송한다
        pts = [m.PointStruct(id=r["chunk_id"], vector=v.tolist(),
                             payload={k: r[k] for k in META_COLS if k != "chunk_id"}) for r, v in zip(rows, vecs)]
        batches = [pts[i:i + 512] for i in range(0, len(pts), 512)]
        list(self.pool.map(lambda b: self.c.upsert(COLLECTION, points=b, wait=True), batches))

    def finalize(self):
        import time
        from qdrant_client import models as m
        t0 = time.perf_counter()
        # threshold(KB)보다 작은 세그먼트는 인덱싱하지 않으므로 1로 내려 전 세그먼트에 HNSW를 만든다
        self.c.update_collection(COLLECTION, optimizers_config=m.OptimizersConfigDiff(indexing_threshold=1))
        while True:
            info = self.c.get_collection(COLLECTION)
            if info.status == m.CollectionStatus.GREEN and (info.indexed_vectors_count or 0) >= (info.points_count or 0) * 0.99:
                break
            if time.perf_counter() - t0 > 6 * 3600:
                raise TimeoutError(f"Qdrant 인덱싱 미완료: {info.indexed_vectors_count}/{info.points_count}")
            time.sleep(2)
        return {"hnsw_optimize": round(time.perf_counter() - t0, 2)}

    def size_bytes(self):
        # WAL(미리 할당되는 로그)은 제외 - pgvector·OpenSearch 크기도 WAL/translog를 빼고 잰다
        out = subprocess.run(["docker", "exec", "rag-qdrant-1", "du", "-sb", "--exclude=wal",
                              f"/qdrant/storage/collections/{COLLECTION}"],
                             capture_output=True, text=True)
        try:
            return {"total": int(out.stdout.split()[0])}
        except Exception:
            return {"total": None}

    def count(self):
        return self.c.count(COLLECTION, exact=True).count

    def set_ef(self, ef):
        self.ef = int(ef)

    def search(self, vec, k=10, flt=None):
        from qdrant_client import models as m
        f = None
        if flt:
            f = m.Filter(must=[
                m.FieldCondition(key="stock_codes", match=m.MatchValue(value=flt["stock_code"])),
                m.FieldCondition(key="published_at", range=m.DatetimeRange(gte=flt["since"])),
            ])
        res = self.c.query_points(COLLECTION, query=vec.tolist(), limit=k, query_filter=f,
                                  search_params=m.SearchParams(hnsw_ef=self.ef), with_payload=False)
        return [str(p.id) for p in res.points]

    def clone(self):
        c = Qdrant(); c.ef = self.ef
        return c


# ---- OpenSearch ---------------------------------------------------------------

class OpenSearch:
    name = "opensearch"

    def __init__(self):
        from opensearchpy import OpenSearch as OS
        from concurrent.futures import ThreadPoolExecutor
        self.c = OS(OPENSEARCH_URL, timeout=600, pool_maxsize=32)
        self.ef = 100
        self.pool = ThreadPoolExecutor(4)

    def create(self):
        if self.c.indices.exists(index=COLLECTION):
            self.c.indices.delete(index=COLLECTION)
        self.c.indices.create(index=COLLECTION, body={
            "settings": {
                "index": {"knn": True, "number_of_shards": 1, "number_of_replicas": 0,
                          "refresh_interval": "-1"},          # 적재 중 refresh 끔
                "analysis": {"analyzer": {"ko": {"type": "custom", "tokenizer": "nori_tokenizer",
                                                 "filter": ["lowercase", "nori_part_of_speech"]}}},
            },
            "mappings": {"properties": {
                "embedding": {"type": "knn_vector", "dimension": EMBED_DIM,
                              "method": {"name": "hnsw", "engine": "faiss", "space_type": "innerproduct",
                                         "parameters": {"m": HNSW_M, "ef_construction": HNSW_EF_CONSTRUCTION}}},
                "text": {"type": "text", "analyzer": "ko"},
                "title": {"type": "text", "analyzer": "ko"},
                "doc_id": {"type": "keyword"}, "chunk_id": {"type": "keyword"},
                "source_type": {"type": "keyword"}, "stock_codes": {"type": "keyword"},
                "stock_names": {"type": "keyword"}, "publisher": {"type": "keyword"},
                "author": {"type": "keyword"}, "section": {"type": "keyword"},
                "published_at": {"type": "date"}, "crawled_at": {"type": "date"},
                "url": {"type": "keyword", "index": False}, "original_url": {"type": "keyword", "index": False},
                "stock_match": {"type": "keyword", "index": False},
                "corp_name": {"type": "keyword"}, "report_nm": {"type": "keyword"}, "rcept_no": {"type": "keyword"},
                "chunk_index": {"type": "integer"}, "n_chunks": {"type": "integer"},
                "char_start": {"type": "integer"}, "char_end": {"type": "integer"},
                "n_tokens": {"type": "integer"}, "n_dup_sources": {"type": "integer"},
            }},
        })

    def load_docs(self, docs):
        pass

    def load(self, rows, vecs):
        from opensearchpy import helpers

        acts = [{"_index": COLLECTION, "_id": r["chunk_id"],
                 "_source": {**{k: r[k] for k in META_COLS}, "embedding": v.tolist()}}
                for r, v in zip(rows, vecs)]
        # Qdrant와 같은 조건: 512건 단위 요청을 스레드 4개로 병렬 전송
        batches = [acts[i:i + 512] for i in range(0, len(acts), 512)]
        list(self.pool.map(lambda b: helpers.bulk(self.c, b, chunk_size=512, max_chunk_bytes=100 * 1024 * 1024,
                                                  raise_on_error=True), batches))

    def finalize(self):
        import time
        t = {}
        t0 = time.perf_counter()
        self.c.indices.put_settings(body={"index": {"refresh_interval": "1s"}}, index=COLLECTION)
        self.c.indices.refresh(index=COLLECTION)
        t["refresh"] = round(time.perf_counter() - t0, 2)
        # 세그먼트를 하나로 합쳐야 HNSW 그래프가 하나가 된다 (다른 저장소와 같은 조건)
        t0 = time.perf_counter()
        self.c.indices.forcemerge(index=COLLECTION, max_num_segments=1, request_timeout=7200)
        t["forcemerge"] = round(time.perf_counter() - t0, 2)
        t0 = time.perf_counter()
        self.c.transport.perform_request("GET", f"/_plugins/_knn/warmup/{COLLECTION}")
        t["warmup"] = round(time.perf_counter() - t0, 2)
        return t

    def size_bytes(self):
        st = self.c.indices.stats(index=COLLECTION, metric="store")
        return {"total": st["_all"]["primaries"]["store"]["size_in_bytes"]}

    def count(self):
        return self.c.count(index=COLLECTION)["count"]

    def set_ef(self, ef):
        self.ef = int(ef)

    def search(self, vec, k=10, flt=None):
        knn = {"vector": vec.tolist(), "k": k, "method_parameters": {"ef_search": self.ef}}
        if flt:
            # faiss 엔진의 효율적 필터링: knn 안에 filter를 넣는다 (사후 필터 아님)
            knn["filter"] = {"bool": {"filter": [
                {"term": {"stock_codes": flt["stock_code"]}},
                {"range": {"published_at": {"gte": flt["since"]}}}]}}
        body = {"size": k, "_source": False, "query": {"knn": {"embedding": knn}}}
        res = self.c.search(index=COLLECTION, body=body)
        return [h["_id"] for h in res["hits"]["hits"]]

    def clone(self):
        c = OpenSearch(); c.ef = self.ef
        return c


STORES = {"pgvector": PgVector, "qdrant": Qdrant, "opensearch": OpenSearch}
