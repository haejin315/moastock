"""6단계: 저장소 적재. 적재 시간·인덱스 빌드 시간·디스크 크기를 기록한다.

  python -m moarag.load pgvector|qdrant|opensearch|all [--batch 5000]
"""
from __future__ import annotations

import argparse
import json
import time

import numpy as np
import pyarrow.parquet as pq

from .config import BENCH_DIR, CHUNKS_PATH, DOCS_PATH, EMB_PATH
from .embed import load_done_mask
from .metrics import Progress, Stage
from .stores import META_COLS, STORES


def run(name: str, batch: int):
    store = STORES[name]()
    emb = np.load(EMB_PATH, mmap_mode="r")
    pf = pq.ParquetFile(CHUNKS_PATH)
    n = pf.metadata.num_rows
    assert emb.shape[0] == n, f"임베딩 {emb.shape[0]} != 청크 {n} - embed 단계를 먼저 돌리세요"
    done = load_done_mask(n)            # 임베딩이 끝난 행만 적재 (정기보고서 후순위 처리 중일 수 있음)
    n_load = int(done.sum())
    result = {"store": name, "chunks_total": n, "chunks": n_load}

    with Stage(f"load_{name}", chunks=n_load, chunks_total=n, batch=batch) as st:
        store.create()
        t0 = time.perf_counter()
        # 문서 전문은 적재 대상 청크가 있는 문서만 (배치로 흘려 넣는다)
        want = set()
        off = 0
        for rb in pf.iter_batches(batch_size=200_000, columns=["doc_id"]):
            ids = rb.column(0).to_pylist()
            m = done[off:off + len(ids)]
            want.update(d for d, ok in zip(ids, m) if ok)
            off += len(ids)
        for rb in pq.ParquetFile(DOCS_PATH).iter_batches(batch_size=20_000):
            docs = [d for d in rb.to_pylist() if d["doc_id"] in want]
            if docs:
                store.load_docs(docs)
        result["docs"] = len(want)
        result["docs_seconds"] = round(time.perf_counter() - t0, 2)

        t0 = time.perf_counter()
        prog = Progress(f"{name} 적재", total=n_load)
        off = 0
        for rb in pf.iter_batches(batch_size=batch, columns=META_COLS):
            k = rb.num_rows
            idx = np.nonzero(done[off:off + k])[0]
            if len(idx):
                rows = rb.take(idx).to_pylist()
                store.load(rows, np.asarray(emb[off + idx]))
                st.add(chunks=len(rows))
                prog.tick(len(rows))
            off += k
        result["ingest_seconds"] = round(time.perf_counter() - t0, 2)
        result["ingest_per_sec"] = round(n_load / result["ingest_seconds"], 1)

        t0 = time.perf_counter()
        result["finalize_detail"] = store.finalize()
        result["finalize_seconds"] = round(time.perf_counter() - t0, 2)
        result["time_to_searchable_seconds"] = round(result["ingest_seconds"] + result["finalize_seconds"], 2)
        result["count"] = store.count()
        result["size_bytes"] = store.size_bytes()
        st.set(**{k: v for k, v in result.items() if k not in ("store",)})
    BENCH_DIR.mkdir(exist_ok=True)
    (BENCH_DIR / f"load_{name}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("store", choices=[*STORES, "all"])
    ap.add_argument("--batch", type=int, default=5000)
    a = ap.parse_args()
    for s in (STORES if a.store == "all" else [a.store]):
        run(s, a.batch)
