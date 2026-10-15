"""매주 RAG 평가: Kubeflow Pipelines 의 moastock-rag-eval 을 실행하고 끝날 때까지 기다린다.

  submit_kfp_run ─▶ (KFP: run_eval ─▶ gate)

- 파이프라인 정의: pipelines/rag_eval.py → 컴파일한 YAML 을 deploy.sh 가 dags/pipelines/ 에 복사한다
- KFP REST API(v2beta1)로 바로 부른다 - Airflow 이미지에 kfp SDK 를 넣지 않으려고
- gate 가 회귀를 찾으면 KFP 실행이 실패하고, 이 작업도 실패로 끝난다. 결과는 eval.runs → dbt eval_history
수동 실행: 화면에서 Trigger (conf 로 {"limit": 3} 처럼 문항 수를 줄일 수 있다)
"""
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pendulum
import requests
import yaml
from airflow.sdk import DAG, task

KST = pendulum.timezone("Asia/Seoul")
KFP = "http://ml-pipeline.kubeflow.svc.cluster.local:8888/apis/v2beta1"
PACKAGE = Path(__file__).parent / "pipelines" / "rag_eval.yaml"


def pipeline_body(path: Path) -> dict:
    """컴파일 YAML (파이프라인 스펙 + 쿠버네티스 플랫폼 스펙 두 문서) → API 가 받는 모양 (kfp Client 와 같게)"""
    docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]
    return docs[0] if len(docs) == 1 else {"pipeline_spec": docs[0], "platform_spec": docs[1]}


with DAG(
    dag_id="moastock_eval_weekly",
    description="Kubeflow Pipelines 로 RAG 평가 42문항 → 지표 기록·회귀 감지",
    schedule="0 16 * * 0",                      # 일요일 16시 (VM 운영 시간 안)
    start_date=datetime(2026, 10, 15, tzinfo=KST),
    catchup=False,
    max_active_runs=1,
    default_args={"owner": "moastock", "retries": 0},
    params={"limit": 0, "max_drop": 0.05},
    tags=["moastock", "eval", "kubeflow"],
) as dag:

    @task(execution_timeout=timedelta(hours=2))
    def submit_kfp_run(params=None):
        tag = f"kfp-{datetime.now(KST):%Y%m%d-%H%M}"
        body = {
            "display_name": f"rag-eval {tag}",
            "pipeline_spec": pipeline_body(PACKAGE),
            "runtime_config": {"parameters": {"tag": tag, "limit": int(params["limit"]),
                                              "max_drop": float(params["max_drop"])}},
        }
        r = requests.post(f"{KFP}/runs", json=body, timeout=60)
        r.raise_for_status()
        kfp_run = r.json()["run_id"]
        print(f"KFP 실행 {kfp_run} ({tag})", flush=True)
        while True:
            time.sleep(30)
            st = requests.get(f"{KFP}/runs/{kfp_run}", timeout=30).json()
            state = st.get("state")
            print("상태:", state, flush=True)
            if state in ("SUCCEEDED", "FAILED", "SKIPPED", "CANCELED"):
                break
        if state != "SUCCEEDED":
            raise RuntimeError(f"KFP 실행 {kfp_run} {state}: {json.dumps(st.get('error'), ensure_ascii=False)}")
        return {"kfp_run": kfp_run, "tag": tag}

    submit_kfp_run()
