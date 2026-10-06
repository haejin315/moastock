"""7단계: 저장소 성능 비교.

  python -m moarag.bench [--stores pgvector,qdrant,opensearch] [--queries 300] [--ef 40,100,200]

측정 항목 (top-k=10, 같은 질의 집합, 같은 클라이언트 머신):
  - 단일 스레드 지연시간 p50/p95/p99/평균 (ms) - 비필터 / 필터(종목 + 최근 90일)
  - recall@10: numpy 전수 탐색(정확한 코사인 top-10) 대비 일치율
  - 동시성 처리량(QPS): 스레드 1/8/16, 스레드마다 별도 클라이언트
ef_search(=hnsw_ef)를 바꿔가며 속도-정확도 곡선을 본다.

질의 집합 (시드 고정):
  - 제목형: 무작위 문서 제목 → "query: {제목}"
  - 질문형: 무작위 종목 × 템플릿 ("{종목} 실적 전망" 등) → 필터 검색에도 같은 종목 사용
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import threading
import time
from datetime import timedelta

import numpy as np
import psutil
import pyarrow.parquet as pq

from .config import BENCH_DIR, CHUNKS_PATH, EMB_PATH, EMBED_MODEL, NOW_KST
from .embed import load_done_mask
from .metrics import Stage
from .stores import STORES

K = 10
TEMPLATES = ["{n} 실적 전망", "{n} 주가 하락 이유", "{n} 신규 수주 계약", "{n} 유상증자 공시",
             "{n} 배당 결정", "{n} 최대주주 지분 변동", "{n} 신사업 투자 계획"]


def build_queries(n_q: int, rows: np.ndarray, seed=7):
    """rows: 저장소에 적재된 행 번호 (임베딩 완료 행)"""
    rnd = random.Random(seed)
    meta = pq.read_table(CHUNKS_PATH, columns=["title", "stock_codes", "stock_names", "published_at"]).take(rows)
    titles = meta.column("title").to_pylist()
    codes = meta.column("stock_codes").to_pylist()
    names = meta.column("stock_names").to_pylist()
    pub = meta.column("published_at").to_pylist()
    since = (NOW_KST - timedelta(days=90)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    # 필터 질의는 최근 90일에 청크가 5개 이상 있는 종목에서 고른다 (정답이 비지 않도록)
    cnt: dict[str, int] = {}
    name_of: dict[str, str] = {}
    for cs, ns, p in zip(codes, names, pub):
        if p and p >= since:
            for c, nm in zip(cs or [], ns or []):
                cnt[c] = cnt.get(c, 0) + 1
                name_of[c] = nm
    pool = sorted(c for c, v in cnt.items() if v >= 5)
    qs = []
    for i in rnd.sample(range(len(titles)), n_q // 2):
        qs.append({"kind": "title", "text": titles[i], "filter": None})
    for _ in range(n_q - len(qs)):
        c = rnd.choice(pool)
        qs.append({"kind": "question", "text": rnd.choice(TEMPLATES).format(n=name_of[c]),
                   "filter": {"stock_code": c, "since": since}})
    return qs, codes, pub


def ground_truth(emb, qvec, qs, codes, pub):
    """정확한 코사인 top-K (임베딩이 정규화돼 있어 내적 = 코사인)."""
    scores = emb @ qvec.T                       # (N, Q)
    gt, gt_f = [], []
    for j, q in enumerate(qs):
        s = scores[:, j]
        gt.append(np.argpartition(-s, K)[:K])
        if q["filter"]:
            f = q["filter"]
            mask = np.fromiter(((f["stock_code"] in (c or [])) and (p or "") >= f["since"]
                                for c, p in zip(codes, pub)), dtype=bool, count=len(codes))
            idx = np.nonzero(mask)[0]
            top = idx[np.argsort(-s[idx])[:K]]
            gt_f.append(top)
        else:
            gt_f.append(None)
    return gt, gt_f


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def lat_summary(ms):
    return {"p50": round(pct(ms, 50), 2), "p95": round(pct(ms, 95), 2), "p99": round(pct(ms, 99), 2),
            "mean": round(statistics.fmean(ms), 2), "n": len(ms)}


def run_latency(store, qvec, qs, ids, gt, use_filter):
    ms, recalls = [], []
    for j, q in enumerate(qs):
        if use_filter and not q["filter"]:
            continue
        truth = gt[j]
        t0 = time.perf_counter()
        got = store.search(qvec[j], K, q["filter"] if use_filter else None)
        ms.append((time.perf_counter() - t0) * 1000)
        want = {ids[i] for i in truth}
        if want:
            recalls.append(len(want & set(got)) / len(want))
    return {**lat_summary(ms), "recall@10": round(statistics.fmean(recalls), 4)}


def run_qps(store, qvec, qs, threads, seconds=15.0):
    clients = [store.clone() for _ in range(threads)]
    stop = time.perf_counter() + seconds
    done = [0] * threads

    def work(t):
        c, j = clients[t], t
        while time.perf_counter() < stop:
            c.search(qvec[j % len(qs)], K, None)
            done[t] += 1
            j += threads

    ths = [threading.Thread(target=work, args=(t,)) for t in range(threads)]
    t0 = time.perf_counter()
    [th.start() for th in ths]
    [th.join() for th in ths]
    return round(sum(done) / (time.perf_counter() - t0), 1)


def main(stores, n_q, efs, qps_threads):
    from sentence_transformers import SentenceTransformer
    emb_all = np.load(EMB_PATH, mmap_mode="r")
    rows = np.nonzero(load_done_mask(emb_all.shape[0]))[0]      # 적재된(임베딩 완료) 행만 정답 후보
    emb = np.asarray(emb_all[rows])
    ids = pq.read_table(CHUNKS_PATH, columns=["chunk_id"]).take(rows).column("chunk_id").to_pylist()
    qs, codes, pub = build_queries(n_q, rows)
    model = SentenceTransformer(EMBED_MODEL, device="cpu")
    qvec = model.encode([f"query: {q['text']}" for q in qs], normalize_embeddings=True,
                        convert_to_numpy=True).astype(np.float32)
    t0 = time.perf_counter()
    gt, gt_f = ground_truth(emb, qvec, qs, codes, pub)
    print(f"정답(전수 탐색) 계산 {time.perf_counter() - t0:.1f}s, 질의 {len(qs)}개 (필터 {sum(1 for q in qs if q['filter'])}개)")

    results = {"chunks": len(ids), "queries": len(qs), "k": K, "model": EMBED_MODEL,
               "host_cpu_percent_at_start": psutil.cpu_percent(interval=2), "stores": {}}
    for name in stores:
        store = STORES[name]()
        with Stage(f"bench_{name}", queries=len(qs), efs=efs) as st:
            r = {"count": store.count(), "by_ef": {}}
            for ef in efs:
                store.set_ef(ef)
                for j in range(min(30, len(qs))):           # 워밍업 (캐시·커넥션)
                    store.search(qvec[j], K, None)
                row = {"unfiltered": run_latency(store, qvec, qs, ids, gt, False),
                       "filtered": run_latency(store, qvec, qs, ids, gt_f, True),
                       "qps": {str(t): run_qps(store, qvec, qs, t) for t in qps_threads}}
                r["by_ef"][str(ef)] = row
                print(f"  {name} ef={ef}: {json.dumps(row, ensure_ascii=False)}", flush=True)
            st.set(result=r)
        results["stores"][name] = r
    BENCH_DIR.mkdir(exist_ok=True)
    out = BENCH_DIR / "bench_results.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"저장: {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stores", default="pgvector,qdrant,opensearch")
    ap.add_argument("--queries", type=int, default=300)
    ap.add_argument("--ef", default="40,100,200")
    ap.add_argument("--qps-threads", default="1,8,16")
    a = ap.parse_args()
    main(a.stores.split(","), a.queries, [int(x) for x in a.ef.split(",")],
         [int(x) for x in a.qps_threads.split(",")])
