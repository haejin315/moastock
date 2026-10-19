"""매주 AI 비서 점검: 사이트 경로 점검 → RAG 평가 42문항 → 회귀 검사. 일요일 16시 (VM 운영 시간 안).

  probe ─▶ rag_eval (run_eval ▶ gate)

- probe:    공개 API(/api/assistant)로 질문 5개 - Cloudflare·터널·assistant·LLM 전 경로 (assistant/eval/probe.py)
- rag_eval: 평가 문항을 클러스터 안에서 돌리고(run_eval.py), 요약을 eval.runs 에 남긴다(gate.py).
            같은 문항 수의 직전 통과 회차보다 max_drop 넘게 나빠지거나 없는 번호를 인용하면 실패 → 실패 알림
probe 가 실패해도 평가는 돈다 (원인이 바깥 경로인지 RAG 인지 나눠 보려고). 결과 → dbt 모델 analytics.eval_history
수동 실행: 화면에서 Trigger (conf 로 {"limit": 3} 처럼 문항 수를 줄일 수 있다)
2026-10-19 까지는 Kubeflow Pipelines(평가)·Vertex AI Pipelines(경로 점검)로 돌렸다.
"""
from datetime import datetime

import pendulum
from airflow.sdk import DAG

from moastock_common import DEFAULT_ARGS, DSN, pod

KST = pendulum.timezone("Asia/Seoul")
APP_ENV = {"ASSIST_LLM_BASE_URL": "http://llm:8000/v1", "ASSIST_PG_DSN": DSN, "MOARAG_EMBED_URL": "http://embed:80",
           "ASSIST_RERANK_URL": "http://rerank:80", "PYTHONUNBUFFERED": "1"}

with DAG(
    dag_id="moastock_eval_weekly",
    description="사이트 경로 점검 + RAG 평가 42문항 → 지표 기록·회귀 감지",
    schedule="0 16 * * 0",
    start_date=datetime(2026, 10, 15, tzinfo=KST),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS | {"retries": 0},
    params={"limit": 0, "max_drop": 0.05},
    tags=["moastock", "eval"],
) as dag:
    probe = pod("probe", "assistant", ["sh", "-c", "cd /app/assistant && python eval/probe.py"],
                mem="512Mi", timeout_min=20)
    rag_eval = pod(
        "rag_eval", "assistant",
        ["sh", "-c",                       # 회차 이름은 실행 시각 (Airflow 3 수동 실행엔 logical_date 가 없다)
         'cd /app/assistant && T="af-$(TZ=Asia/Seoul date +%Y%m%d-%H%M)" L="{{ params.limit }}" && '
         'python eval/run_eval.py --tag "$T" --out /tmp/eval.json $([ "$L" != 0 ] && echo --limit "$L") && '
         'python eval/gate.py /tmp/eval.json --tag "$T" --max-drop "{{ params.max_drop }}"'],
        env=APP_ENV, mem="4Gi", timeout_min=120, trigger_rule="all_done")

    probe >> rag_eval
