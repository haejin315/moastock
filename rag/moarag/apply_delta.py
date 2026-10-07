"""redo_dart build 가 만든 delta(새로 가공한 공시 문서·청크)를 운영 중인 pgvector 에 반영한다 (서버에서 실행).

운영 중단 없이: 새 청크 표(chunk_new)를 옆에 만들어 채우고 인덱스까지 만든 뒤, 한 트랜잭션에서 표 이름을 바꿔 끼운다.

  python -m moarag.apply_delta prepare     # chunk_new 생성 + 바뀌지 않은 청크 복사
  python -m moarag.apply_delta embed       # delta 청크 임베딩(MOARAG_EMBED_URL) → chunk_new  (끊겨도 이어서)
  python -m moarag.apply_delta index [--mem 8GB] [--workers 3]   # chunk_new 인덱스
  python -m moarag.apply_delta swap        # 문서 본문 갱신 + chunk ↔ chunk_new 교체 + 옛 표 삭제

delta 파일 위치: $MOARAG_DATA_DIR/delta/{docs,chunks}.parquet
"""
from __future__ import annotations

import argparse
import time
import uuid

import numpy as np
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from pgvector.psycopg import register_vector

from .config import DATA_DIR, HNSW_EF_CONSTRUCTION, HNSW_M, PG_DSN
from .embed import make_encoder, passage_texts
from .metrics import Progress, Stage
from .stores import _ts

DELTA = DATA_DIR / "delta"
COLS = ["chunk_id", "doc_id", "chunk_index", "char_start", "char_end", "n_tokens", "content", "source_type",
        "published_at", "stock_codes", "embedding", "kind"]


def _con():
    c = psycopg.connect(PG_DSN, autocommit=True)
    register_vector(c)
    return c


def _delta_doc_ids() -> list[str]:
    return pq.read_table(DELTA / "docs.parquet", columns=["doc_id"]).column(0).to_pylist()


def prepare():
    con = _con()
    ids = _delta_doc_ids()
    with Stage("apply_prepare", delta_docs=len(ids)) as st:
        con.execute("ALTER TABLE chunk ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'text'")
        con.execute("DROP TABLE IF EXISTS chunk_new")
        con.execute("CREATE TABLE chunk_new (LIKE chunk INCLUDING DEFAULTS INCLUDING CONSTRAINTS)")
        con.execute("CREATE TEMP TABLE delta_doc (doc_id text PRIMARY KEY)")
        with con.cursor().copy("COPY delta_doc (doc_id) FROM STDIN") as cp:
            for d in ids:
                cp.write_row([d])
        t0 = time.perf_counter()
        n = con.execute("INSERT INTO chunk_new SELECT c.* FROM chunk c "
                        "WHERE NOT EXISTS (SELECT 1 FROM delta_doc d WHERE d.doc_id = c.doc_id)").rowcount
        st.set(kept_chunks=n, copy_seconds=round(time.perf_counter() - t0, 1))
    print(f"바뀌지 않은 청크 {n:,}개 복사", flush=True)


def embed(block: int, batch: int):
    con = _con()
    pf = pq.ParquetFile(DELTA / "chunks.parquet")
    total = pf.metadata.num_rows
    have = {str(r[0]) for r in con.execute(
        "SELECT chunk_id FROM chunk_new WHERE doc_id = ANY(%s)", (_delta_doc_ids(),)).fetchall()}
    enc = make_encoder()
    cols = ["chunk_id", "doc_id", "chunk_index", "char_start", "char_end", "n_tokens", "text", "title",
            "source_type", "published_at", "stock_codes", "kind"]
    with Stage("apply_embed", chunks=total, already=len(have), batch=batch) as st:
        prog = Progress("delta 임베딩", total=total - len(have), every=60)
        for rb in pf.iter_batches(batch_size=block, columns=cols):
            rows = [r for r in rb.to_pylist() if r["chunk_id"] not in have]
            if not rows:
                continue
            vecs = enc.encode(passage_texts(pa.Table.from_pylist(rows)), batch)
            with con.transaction(), con.cursor().copy(
                    f"COPY chunk_new ({','.join(COLS)}) FROM STDIN WITH (FORMAT BINARY)") as cp:
                cp.set_types(["uuid", "text", "int4", "int4", "int4", "int4", "text", "text", "timestamptz",
                              "text[]", "vector", "text"])
                for r, v in zip(rows, vecs):
                    cp.write_row([uuid.UUID(r["chunk_id"]), r["doc_id"], r["chunk_index"], r["char_start"],
                                  r["char_end"], r["n_tokens"], r["text"], r["source_type"], _ts(r["published_at"]),
                                  r["stock_codes"] or [], np.asarray(v, dtype=np.float32), r["kind"] or "text"])
            st.add(chunks=len(rows))
            prog.tick(len(rows))
    left = total - con.execute("SELECT count(*) FROM chunk_new WHERE doc_id = ANY(%s)",
                               (_delta_doc_ids(),)).fetchone()[0]
    print(f"남은 delta 청크 {left:,}", flush=True)


def index(mem: str, workers: int):
    con = _con()
    con.execute(f"SET maintenance_work_mem = '{mem}'")
    con.execute(f"SET max_parallel_maintenance_workers = {int(workers)}")
    steps = [
        ("pkey", "ALTER TABLE chunk_new ADD CONSTRAINT chunk_new_pkey PRIMARY KEY (chunk_id)"),
        ("fk_doc", "ALTER TABLE chunk_new ADD CONSTRAINT chunk_new_doc_id_fkey FOREIGN KEY (doc_id) "
                   "REFERENCES doc(doc_id) ON DELETE CASCADE"),
        ("btree_doc", "CREATE INDEX chunk_new_doc_id ON chunk_new (doc_id)"),
        ("btree_date", "CREATE INDEX chunk_new_published_at ON chunk_new (published_at)"),
        ("gin_stock", "CREATE INDEX chunk_new_stock_codes ON chunk_new USING gin (stock_codes)"),
        ("hnsw", f"CREATE INDEX chunk_new_embedding_hnsw ON chunk_new USING hnsw (embedding vector_cosine_ops) "
                 f"WITH (m={HNSW_M}, ef_construction={HNSW_EF_CONSTRUCTION})"),
        ("analyze", "ANALYZE chunk_new"),
    ]
    existing = {r[0] for r in con.execute(
        "SELECT conname FROM pg_constraint WHERE conrelid = 'chunk_new'::regclass "
        "UNION SELECT indexname FROM pg_indexes WHERE tablename = 'chunk_new'")}
    with Stage("apply_index", mem=mem, workers=workers) as st:
        for label, sql in steps:
            name = sql.split(" ADD CONSTRAINT ")[-1].split()[0] if "ADD CONSTRAINT" in sql else \
                sql.split("CREATE INDEX ")[-1].split()[0] if "CREATE INDEX" in sql else None
            if name and name in existing:
                continue                                   # 이어서 실행할 때 이미 만든 것은 건너뛴다
            t0 = time.perf_counter()
            con.execute(sql)
            st.set(**{f"{label}_seconds": round(time.perf_counter() - t0, 1)})
            print(f"  {label} {time.perf_counter() - t0:.0f}s", flush=True)


def swap():
    con = _con()
    docs = pq.read_table(DELTA / "docs.parquet", columns=["doc_id", "text", "content_sha1"]).to_pylist()
    n_new = con.execute("SELECT count(*) FROM chunk_new").fetchone()[0]
    with Stage("apply_swap", docs=len(docs), chunks=n_new) as st:
        with con.transaction():
            con.execute("CREATE TEMP TABLE delta_body (doc_id text PRIMARY KEY, body text, sha text) ON COMMIT DROP")
            with con.cursor().copy("COPY delta_body (doc_id, body, sha) FROM STDIN") as cp:
                for d in docs:
                    cp.write_row([d["doc_id"], d["text"], d["content_sha1"]])
            con.execute("UPDATE doc SET body = b.body, content_sha1 = b.sha FROM delta_body b WHERE doc.doc_id = b.doc_id")
            # 옛 표·인덱스 이름을 비켜 두고 새 것을 원래 이름으로
            con.execute("ALTER TABLE chunk RENAME TO chunk_old")
            for idx in ("chunk_pkey", "chunk_doc_id", "chunk_published_at", "chunk_stock_codes", "chunk_embedding_hnsw"):
                con.execute(f"ALTER INDEX IF EXISTS {idx} RENAME TO {idx}_old")
            con.execute("ALTER TABLE chunk_old RENAME CONSTRAINT chunk_doc_id_fkey TO chunk_doc_id_fkey_old")
            con.execute("ALTER TABLE chunk_new RENAME TO chunk")
            for idx in ("chunk_doc_id", "chunk_published_at", "chunk_stock_codes", "chunk_embedding_hnsw"):
                con.execute(f"ALTER INDEX chunk_new_{idx.removeprefix('chunk_')} RENAME TO {idx}")
            con.execute("ALTER TABLE chunk RENAME CONSTRAINT chunk_new_pkey TO chunk_pkey")
            con.execute("ALTER TABLE chunk RENAME CONSTRAINT chunk_new_doc_id_fkey TO chunk_doc_id_fkey")
        con.execute("DROP TABLE chunk_old")
        con.execute("ANALYZE doc")
        st.set(chunks=con.execute("SELECT count(*) FROM chunk").fetchone()[0])
    print("교체 완료", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prepare")
    e = sub.add_parser("embed")
    e.add_argument("--block", type=int, default=5000)
    e.add_argument("--batch", type=int, default=128)
    i = sub.add_parser("index")
    i.add_argument("--mem", default="1GB")
    i.add_argument("--workers", type=int, default=0)
    sub.add_parser("swap")
    a = ap.parse_args()
    {"prepare": prepare, "swap": swap}.get(a.cmd, lambda: None)()
    if a.cmd == "embed":
        embed(a.block, a.batch)
    elif a.cmd == "index":
        index(a.mem, a.workers)
