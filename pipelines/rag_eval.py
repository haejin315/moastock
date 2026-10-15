"""Kubeflow Pipelines 정의 (KFP SDK v2). 같은 SDK 로 두 곳에서 돌린다.

1. moastock-rag-eval  ─ k3s 의 Kubeflow Pipelines (Airflow DAG moastock_eval_weekly 가 매주 실행)
     run_eval ─▶ gate
   - run_eval: 평가 42문항을 클러스터 안에서 돌린다 (오케스트레이터 코드를 그대로, LLM·DB·리랭커는 클러스터 서비스)
   - gate:     요약 지표를 eval.runs 에 남기고, 직전 통과 회차보다 정해 둔 폭 넘게 나빠지면 실패 (회귀 감지)

2. moastock-e2e-probe ─ Vertex AI Pipelines (관리형 Kubeflow, 매주 일정)
   - probe: 바깥(Google 클라우드)에서 사이트 공개 API(/api/assistant)로 질문 몇 개를 보내
            응답 여부·시간·인용·SSE 형식을 점검한다. 터널·Worker·k3s 까지 사용자 경로 전체를 본다.

  python pipelines/rag_eval.py      → pipelines/compiled/*.yaml
"""
from pathlib import Path

from kfp import compiler, dsl
from kfp.dsl import Artifact, Input, Metrics, Output

APP_IMAGE = "moastock/assistant:current"       # VM 에서 빌드한 오케스트레이터 이미지
SVC = "moastock.svc.cluster.local"             # 파이프라인 파드는 kubeflow 네임스페이스에서 돈다


@dsl.container_component
def run_eval(tag: str, limit: int, results: Output[Artifact]):
    return dsl.ContainerSpec(
        image=APP_IMAGE,
        command=["sh", "-c", 'cd /app/assistant && python eval/run_eval.py --tag "$0" --out "$1" ${2:+--limit $2}'],
        args=[tag, results.path, limit],
    )


@dsl.component(base_image=APP_IMAGE)
def gate(results: Input[Artifact], tag: str, source: str, metrics: Output[Metrics], max_drop: float = 0.05) -> bool:
    """직전 통과 회차 대비 회귀 검사. 결과는 eval.runs 에 남긴다 (dbt 모델 eval_history 가 읽는다)."""
    import json
    import os

    import psycopg

    s = json.load(open(results.path, encoding="utf-8"))["summary"]
    for k, v in s.items():
        if isinstance(v, (int, float)) and v is not None:
            metrics.log_metric(k, float(v))
    con = psycopg.connect(os.environ["ASSIST_PG_DSN"], autocommit=True)
    con.execute("CREATE SCHEMA IF NOT EXISTS eval")
    con.execute("""CREATE TABLE IF NOT EXISTS eval.runs (run_id text PRIMARY KEY, source text, tag text,
        created_at timestamptz DEFAULT now(), questions int, route_accuracy double precision,
        dart_hit_evidence double precision, dart_hit_cited double precision, number_support_rate double precision,
        latency_p50_ms int, latency_p95_ms int, passed boolean, summary jsonb)""")
    prev = con.execute("""SELECT route_accuracy, dart_hit_cited, number_support_rate FROM eval.runs
                          WHERE source = %s AND passed ORDER BY created_at DESC LIMIT 1""", (source,)).fetchone()
    reasons = []
    if s.get("answers_with_invalid_citation"):
        reasons.append(f"없는 번호 인용 {s['answers_with_invalid_citation']}건")
    if prev:
        for name, old, new in zip(["route_accuracy", "dart_hit_cited", "number_support_rate"], prev,
                                  [s["route_accuracy"], s["dart_hit_cited"], s["number_support_rate"]]):
            if old is not None and new is not None and new < old - max_drop:
                reasons.append(f"{name} {old:.3f} → {new:.3f}")
    passed = not reasons
    con.execute("""INSERT INTO eval.runs (run_id, source, tag, questions, route_accuracy, dart_hit_evidence,
                   dart_hit_cited, number_support_rate, latency_p50_ms, latency_p95_ms, passed, summary)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (run_id) DO NOTHING""",
                (tag, source, tag, s["questions"], s["route_accuracy"], s["dart_hit@evidence"], s["dart_hit_cited"],
                 s["number_support_rate"], s["latency_ms_p50"], s["latency_ms_p95"], passed,
                 json.dumps(s | {"regressions": reasons}, ensure_ascii=False)))
    metrics.log_metric("passed", float(passed))
    print("통과" if passed else "회귀: " + ", ".join(reasons), flush=True)
    return passed


ENV = {
    "ASSIST_LLM_BASE_URL": f"http://llm.{SVC}:8000/v1",
    "ASSIST_PG_DSN": f"postgresql://moarag:moarag@db.{SVC}:5432/moarag",
    "MOARAG_EMBED_URL": f"http://embed.{SVC}:80",
    "ASSIST_RERANK_URL": f"http://rerank.{SVC}:80",
}


@dsl.pipeline(name="moastock-rag-eval", description="RAG 평가 42문항 → 지표 기록·회귀 감지")
def rag_eval_pipeline(tag: str, limit: int = 0, max_drop: float = 0.05):
    ev = run_eval(tag=tag, limit=limit).set_memory_limit("4G").set_caching_options(False)
    g = gate(results=ev.outputs["results"], tag=tag, source="kfp-local", max_drop=max_drop).set_caching_options(False)
    for t in (ev, g):
        for k, v in ENV.items():
            t.set_env_variable(k, v)
        try:                                       # 로컬 이미지를 쓰게 (kfp-kubernetes 가 있으면)
            from kfp import kubernetes
            kubernetes.set_image_pull_policy(t, "Never")
        except ImportError:
            pass


PROBE_QUESTIONS = ["삼성전자 최근 소식 알려줘", "SK하이닉스 최대주주는?", "현대차 최근 공시 알려줘",
                   "카카오 PER 알려줘", "LG화학 배터리 자회사 최근 실적은?"]


@dsl.component(base_image="python:3.12-slim", packages_to_install=["requests==2.32.3"])
def probe(url: str, questions: list, metrics: Output[Metrics], min_ok: float = 0.8) -> float:
    """사용자와 같은 경로로 질문을 보내고 SSE 를 끝까지 읽는다. 성공 = 오류 없이 done 이벤트까지, 인용 있음."""
    import json
    import statistics
    import time

    import requests

    st = requests.get(url, timeout=20).json()
    if not st.get("open"):
        raise RuntimeError(f"운영 시간이 아님: {st}")
    oks, lat, first = [], [], []
    for q in questions:
        t0, t_first, ok, err = time.time(), None, False, None
        try:
            with requests.post(url, json={"question": q}, stream=True, timeout=180) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code} {r.text[:200]}")
                kind = None
                for line in r.iter_lines(decode_unicode=True):
                    if line.startswith("event:"):
                        kind = line[6:].strip()
                    elif line.startswith("data:"):
                        d = json.loads(line[5:])
                        if kind == "token" and t_first is None:
                            t_first = time.time() - t0
                        if kind == "error":
                            raise RuntimeError(d.get("message"))
                        if kind == "done":
                            ok = bool(d.get("checks", {}).get("has_citation", True))
        except Exception as e:
            err = repr(e)
        oks.append(ok)
        lat.append(time.time() - t0)
        if t_first is not None:
            first.append(t_first)
        print(f"{'OK ' if ok else 'FAIL'} {lat[-1]:5.1f}s {q} {err or ''}", flush=True)
    rate = sum(oks) / len(oks)
    metrics.log_metric("success_rate", rate)
    metrics.log_metric("latency_p50_s", statistics.median(lat))
    if first:
        metrics.log_metric("first_token_p50_s", statistics.median(first))
    if rate < min_ok:
        raise RuntimeError(f"성공률 {rate:.0%} < {min_ok:.0%}")
    return rate


@dsl.pipeline(name="moastock-e2e-probe", description="사이트 공개 API 로 AI 비서 전 경로 점검 (Vertex AI Pipelines)")
def e2e_probe_pipeline(url: str = "https://moastock.co.kr/api/assistant", min_ok: float = 0.8):
    probe(url=url, questions=PROBE_QUESTIONS, min_ok=min_ok).set_caching_options(False).set_retry(0)


if __name__ == "__main__":
    out = Path(__file__).parent / "compiled"
    out.mkdir(exist_ok=True)
    compiler.Compiler().compile(rag_eval_pipeline, str(out / "rag_eval.yaml"))
    compiler.Compiler().compile(e2e_probe_pipeline, str(out / "e2e_probe.yaml"))
    import json

    import yaml                                  # Vertex REST API 는 JSON 으로 받는다 (pipelines/vertex.sh)
    (out / "e2e_probe.json").write_text(json.dumps(yaml.safe_load((out / "e2e_probe.yaml").read_text(encoding="utf-8")),
                                                   ensure_ascii=False), encoding="utf-8")
    print("compiled:", *sorted(p.name for p in out.glob("*.*")))
