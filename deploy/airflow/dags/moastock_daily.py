"""매일 증분 파이프라인 (cron 의 daily.sh 를 대신한다). VM 운영 시간(15~22시 KST) 안에 두 번.

  collect ─▶ load ─▶ kg_build ─▶ dbt_build (모델 + 데이터 품질 테스트) ─▶ dbt_freshness

- collect: 네이버 뉴스·DART 공시 수집 → 원천 저장소(raw.sqlite)
- load: 정제·중복 제거·청킹·임베딩 → pgvector
- kg_build: 정기보고서 표 → 회사 관계 그래프(kg_edge)
- dbt_build: 분석 모델(analytics.*, 종목 동시 언급 포함) + 품질 테스트. 테스트가 실패하면 이 작업이 실패해 알림이 간다
스팟 회수로 VM 이 꺼지면 작업 파드가 죽고, 다시 켜진 뒤 재시도(retries=2)로 이어진다. catchup=False 라 꺼져 있던
동안 놓친 회차는 가장 최근 것 하나만 돈다.
"""
from datetime import datetime, timedelta

import pendulum
from airflow.providers.cncf.kubernetes.secret import Secret
from airflow.sdk import DAG
from airflow.timetables.trigger import MultipleCronTriggerTimetable

from moastock_common import DEFAULT_ARGS, DSN, pod

KST = pendulum.timezone("Asia/Seoul")
RAG_ENV = {"MOARAG_DATA_DIR": "/data/rag/daily", "MOARAG_PG_DSN": DSN, "MOARAG_EMBED_URL": "http://embed:80",
           "PYTHONUNBUFFERED": "1"}
DART = Secret("env", "DART_API_KEY", "moastock-env", "DART_API_KEY")
DBT_ENV = {"DBT_PG_HOST": "db", "DBT_PG_USER": "moarag", "DBT_PG_PASSWORD": "moarag", "DBT_PG_DB": "moarag"}

with DAG(
    dag_id="moastock_daily",
    description="뉴스·공시 수집 → 적재 → 관계 그래프 → dbt 모델·품질 테스트",
    schedule=MultipleCronTriggerTimetable("20 15 * * *", "0 21 * * *", timezone=KST),
    start_date=datetime(2026, 10, 15, tzinfo=KST),
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(hours=3),
    default_args=DEFAULT_ARGS,
    tags=["moastock", "etl"],
) as dag:
    collect = pod("collect", "assistant", ["sh", "-c", "cd /app/rag && python -m moarag.daily --step collect"],
                  env=RAG_ENV, secrets=[DART], rag=True, mem="3Gi")
    load = pod("load", "assistant", ["sh", "-c", "cd /app/rag && python -m moarag.daily --step load"],
               env=RAG_ENV, rag=True, mem="6Gi", cpu="250m")
    kg_build = pod("kg_build", "assistant", ["sh", "-c", "cd /app/rag && python -m moarag.kg_build"],
                   env=RAG_ENV, rag=True, mem="4Gi")
    dbt_build = pod("dbt_build", "dbt", ["dbt", "build", "--project-dir", "/app", "--profiles-dir", "/app"],
                    env=DBT_ENV, mem="1Gi", timeout_min=30)
    dbt_freshness = pod("dbt_freshness", "dbt",
                        ["dbt", "source", "freshness", "--project-dir", "/app", "--profiles-dir", "/app"],
                        env=DBT_ENV, mem="512Mi", timeout_min=10, retries=0)

    collect >> load >> kg_build >> dbt_build >> dbt_freshness
