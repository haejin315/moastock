"""남은 청크(정기보고서 등 임베딩이 미뤄진 행)를 임베딩해 이미 운영 중인 pgvector에 덧붙인다.

  MOARAG_EMBED_URL=http://127.0.0.1:8081 python -m moarag.backfill [--block 20000] [--batch 128]

- embed_done.npy 에서 아직 안 된 행만 골라 임베딩 서버(GPU)로 계산한다.
- 문서(doc)는 없는 것만 넣고, 청크는 블록 단위로 넣은 뒤 완료 표시를 저장한다 → 중간에 끊겨도 이어서 실행.
- 많이 덧붙일 때는 HNSW 인덱스를 지우고 다 넣은 뒤 다시 만드는 편이 훨씬 빠르다(--rebuild-index).
"""
from __future__ import annotations

import argparse
import time

import numpy as np
import pyarrow.parquet as pq

from .config import CHUNKS_PATH, DOCS_PATH
from .embed import DONE_PATH, load_done_mask, make_encoder, passage_texts
from .metrics import Progress, Stage
from .stores import PgVector, meta_cols


def main(block: int, batch: int, rebuild_index: bool):
    pf = pq.ParquetFile(CHUNKS_PATH)
    n = pf.metadata.num_rows
    done = load_done_mask(n)
    todo = int((~done).sum())
    print(f"전체 {n:,} / 남은 청크 {todo:,}", flush=True)
    if not todo:
        return

    pg = PgVector()
    have = {r[0] for r in pg.con.execute("SELECT doc_id FROM doc").fetchall()}
    enc = make_encoder()
    if rebuild_index:
        pg.con.execute("DROP INDEX IF EXISTS chunk_embedding_hnsw")

    # 새로 필요한 문서 = 남은 청크가 속한 문서 중 아직 없는 것
    need_docs, off = set(), 0
    for rb in pf.iter_batches(batch_size=200_000, columns=["doc_id"]):
        ids = rb.column(0).to_pylist()
        need_docs.update(d for d, ok in zip(ids, done[off:off + len(ids)]) if not ok and d not in have)
        off += len(ids)
    for rb in pq.ParquetFile(DOCS_PATH).iter_batches(batch_size=20_000):
        docs = [d for d in rb.to_pylist() if d["doc_id"] in need_docs]
        if docs:
            pg.load_docs(docs)
    print(f"문서 추가 {len(need_docs):,}", flush=True)

    with Stage("backfill", chunks=todo, batch=batch, rebuild_index=rebuild_index) as st:
        prog = Progress("덧붙이기", total=todo, every=60)
        off = 0
        for rb in pf.iter_batches(batch_size=block, columns=meta_cols(pf)):
            k = rb.num_rows
            idx = np.nonzero(~done[off:off + k])[0]
            if len(idx):
                sub = rb.take(idx)
                vecs = enc.encode(passage_texts(sub), batch)
                with pg.con.transaction():
                    pg.load(sub.to_pylist(), vecs)
                done[off + idx] = True
                np.save(DONE_PATH, done)          # 넣은 뒤에 표시 - 끊겨도 중복 없이 이어진다
                st.add(chunks=len(idx))
                prog.tick(len(idx))
            off += k
        if rebuild_index:
            from .config import HNSW_EF_CONSTRUCTION, HNSW_M
            t0 = time.perf_counter()
            pg.con.execute(f"CREATE INDEX chunk_embedding_hnsw ON chunk USING hnsw (embedding vector_cosine_ops) "
                           f"WITH (m={HNSW_M}, ef_construction={HNSW_EF_CONSTRUCTION})")
            st.set(index_seconds=round(time.perf_counter() - t0, 1))
        pg.con.execute("ANALYZE doc; ANALYZE chunk")
        st.set(count=pg.count())
    print(f"완료: 청크 {pg.count():,}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--block", type=int, default=20000)
    ap.add_argument("--batch", type=int, default=128)
    ap.add_argument("--rebuild-index", action="store_true")
    a = ap.parse_args()
    main(a.block, a.batch, a.rebuild_index)
