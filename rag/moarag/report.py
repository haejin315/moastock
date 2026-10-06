"""처리량·소요시간·저장소 비교 결과를 하나의 마크다운 보고서로 묶는다.

  python -m moarag.report  →  rag/REPORT.md
"""
from __future__ import annotations

import json

from .config import BENCH_DIR, METRICS_PATH, ROOT

STAGE_ORDER = ["news_section", "news_index", "crawl_news", "dart_list", "dart_docs", "process_clean",
               "process_dedup", "process_write_docs", "process_chunk", "process_merge_chunks", "embed",
               "load_pgvector", "load_qdrant", "load_opensearch"]
# process_chunk는 입력이 바뀌면 처음부터 다시 만들므로 합산하지 않는다(마지막 실행 기준)
RESUMABLE = {"news_section", "news_index", "crawl_news", "dart_list", "dart_docs", "embed"}


def fmt_s(sec):
    sec = float(sec)
    if sec >= 3600:
        return f"{sec / 3600:.2f}h"
    if sec >= 60:
        return f"{sec / 60:.1f}m"
    return f"{sec:.1f}s"


def mb(b):
    return f"{b / 1024 / 1024:,.1f} MB" if b else "-"


def main():
    recs = [json.loads(l) for l in METRICS_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    # 이어받기 단계(남은 것만 처리)는 실행분을 합산하고, 매번 전체를 다시 만드는 단계는 마지막 실행만 쓴다
    agg: dict[str, dict] = {}
    for r in recs:
        if r["status"] != "ok" and not r.get("counts"):
            continue
        if r["stage"] not in RESUMABLE:
            agg.pop(r["stage"], None)
        a = agg.setdefault(r["stage"], {"seconds": 0.0, "counts": {}, "runs": 0})
        a["seconds"] += r["seconds"]
        a["runs"] += 1
        for k, v in r.get("counts", {}).items():
            a["counts"][k] = a["counts"].get(k, 0) + v

    out = ["# moastock RAG 데이터 파이프라인 보고서", "",
           "`python -m moarag.report`로 생성. 원천 수치는 `metrics.jsonl`, `bench/*.json`.", "",
           "## 1. 단계별 처리량·소요시간", "",
           "| 단계 | 실행 | 소요 | 주요 건수 | 처리량 |", "|---|---:|---:|---|---|"]
    for st in STAGE_ORDER + sorted(set(agg) - set(STAGE_ORDER)):
        if st not in agg or st.startswith("bench_"):
            continue
        a = agg[st]
        sec = a["seconds"] or 1e-9
        counts = ", ".join(f"{k}={v:,.0f}" for k, v in a["counts"].items() if not k.endswith("bytes"))
        main_k = next((k for k in ("ok", "kept", "chunks", "links", "docs_out", "news_kept", "rows", "stocks")
                       if k in a["counts"]), None)
        rate = f"{a['counts'][main_k] / sec:,.1f} {main_k}/s" if main_k else "-"
        out.append(f"| {st} | {a['runs']} | {fmt_s(a['seconds'])} | {counts} | {rate} |")

    loads = {p.stem.removeprefix("load_"): json.loads(p.read_text(encoding="utf-8"))
             for p in sorted(BENCH_DIR.glob("load_*.json"))}
    if loads:
        out += ["", "## 2. 저장소 적재", "",
                "| 저장소 | 청크 | 적재 | 적재 처리량 | 인덱스 빌드/최적화 | 검색 가능까지 | 디스크 |",
                "|---|---:|---:|---:|---:|---:|---:|"]
        for name, r in loads.items():
            out.append(f"| {name} | {r['count']:,} | {fmt_s(r['ingest_seconds'])} | {r['ingest_per_sec']:,.0f}/s | "
                       f"{fmt_s(r['finalize_seconds'])} | {fmt_s(r['time_to_searchable_seconds'])} | "
                       f"{mb(r['size_bytes'].get('total'))} |")

    bp = BENCH_DIR / "bench_results.json"
    if bp.exists():
        b = json.loads(bp.read_text(encoding="utf-8"))
        out += ["", "## 3. 검색 성능 비교", "",
                f"청크 {b['chunks']:,}개 · 질의 {b['queries']}개 · top-{b['k']} · 저장소를 하나씩만 띄워 측정", "",
                "recall@10은 정확한 전수 탐색 대비 정답 일치율, 점수 기준은 실제 유사도가 정답 10위 이상이면 정답으로 친 값.", "",
                "| 저장소 | ef | p50 ms | p95 ms | p99 ms | recall@10 | recall(점수) | 필터 p50 | 필터 p95 | 필터 recall | QPS 1/8/16 | 측정 중 CPU |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|"]
        for name, r in b["stores"].items():
            cpu = (r.get("host_cpu_percent") or {}).get("mean")
            for ef, row in r["by_ef"].items():
                u, f = row["unfiltered"], row["filtered"]
                qps = " / ".join(f"{v:,.0f}" for v in row["qps"].values())
                out.append(f"| {name} | {ef} | {u['p50']} | {u['p95']} | {u['p99']} | {u['recall@10']:.3f} | "
                           f"{u.get('recall@10_score', float('nan')):.3f} | {f['p50']} | {f['p95']} | "
                           f"{f['recall@10']:.3f} | {qps} | {cpu if cpu is not None else '-'}% |")
    (ROOT / "REPORT.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"저장: {ROOT / 'REPORT.md'}")


if __name__ == "__main__":
    main()
