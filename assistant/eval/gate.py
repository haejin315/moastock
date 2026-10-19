"""평가 회귀 검사: run_eval.py 결과 요약을 eval.runs 에 남기고, 직전 통과 회차보다 정해 둔 폭 넘게 나빠지면 실패한다.

  python eval/gate.py <결과.json> --tag <회차> [--source airflow] [--max-drop 0.05]

비교 대상은 같은 문항 수의 직전 통과 회차 (3문항 시험 실행과 42문항 정기 실행을 섞어 비교하지 않게).
2026-10-18 까지의 Kubeflow 회차(source kfp-local)도 기준으로 이어 쓴다.
결과는 dbt 모델 analytics.eval_history 가 읽는다. Airflow DAG moastock_eval_weekly 가 매주 부른다.
"""
import argparse
import json
import os
import sys

import psycopg

METRICS = ["route_accuracy", "dart_hit_cited", "number_support_rate"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--source", default="airflow")
    ap.add_argument("--max-drop", type=float, default=0.05)
    a = ap.parse_args()

    s = json.load(open(a.results, encoding="utf-8"))["summary"]
    con = psycopg.connect(os.environ["ASSIST_PG_DSN"], autocommit=True)
    con.execute("CREATE SCHEMA IF NOT EXISTS eval")
    con.execute("""CREATE TABLE IF NOT EXISTS eval.runs (run_id text PRIMARY KEY, source text, tag text,
        created_at timestamptz DEFAULT now(), questions int, route_accuracy double precision,
        dart_hit_evidence double precision, dart_hit_cited double precision, number_support_rate double precision,
        latency_p50_ms int, latency_p95_ms int, passed boolean, summary jsonb)""")
    prev = con.execute(f"""SELECT run_id, {', '.join(METRICS)} FROM eval.runs
                           WHERE questions = %s AND passed ORDER BY created_at DESC LIMIT 1""",
                       (s["questions"],)).fetchone()
    reasons = []
    if s.get("answers_with_invalid_citation"):
        reasons.append(f"없는 번호 인용 {s['answers_with_invalid_citation']}건")
    if prev:
        for name, old in zip(METRICS, prev[1:]):
            new = s.get(name)
            if old is not None and new is not None and new < old - a.max_drop:
                reasons.append(f"{name} {old:.3f} → {new:.3f}")
    passed = not reasons
    con.execute("""INSERT INTO eval.runs (run_id, source, tag, questions, route_accuracy, dart_hit_evidence,
                   dart_hit_cited, number_support_rate, latency_p50_ms, latency_p95_ms, passed, summary)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (run_id) DO NOTHING""",
                (a.tag, a.source, a.tag, s["questions"], s["route_accuracy"], s["dart_hit@evidence"],
                 s["dart_hit_cited"], s["number_support_rate"], s["latency_ms_p50"], s["latency_ms_p95"], passed,
                 json.dumps(s | {"regressions": reasons, "baseline": prev[0] if prev else None}, ensure_ascii=False)))
    print(f"기준 회차: {prev[0] if prev else '없음 (이번 회차가 기준이 된다)'}")
    print("통과" if passed else "회귀: " + ", ".join(reasons), flush=True)
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
