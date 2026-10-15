# GPU 서버 (k3s) 운영

GCP VM `moastock-ai` 한 대(서울, n1-highmem-4 + T4, 스팟, 매일 15~22시) 위의 단일 노드 쿠버네티스.
2026-10-15 에 Docker Compose(`deploy/gcp/docker-compose.yml`, 지금은 쓰지 않음)에서 옮겼다.

```
                         Cloudflare Worker (/api/assistant)
                                   │ Workers VPC → Cloudflare 터널 (공개 IP 없음)
┌─ k3s ─────────────────────────────┼──────────────────────────────────────────────┐
│ ns moastock                       ▼                                              │
│  assistant(+cloudflared) ── llm(vLLM) · embed(TEI) · rerank(TEI) · db(pgvector)  │
│        │ Langfuse(SaaS)로 추적                   GPU 1장을 시간 분할로 셋이 나눔      │
│  airflow ── moastock_daily (하루 2번)                                             │
│     collect ▶ load ▶ kg_build ┐                                                  │
│                   ▶ spark_comention ┴▶ dbt_build ▶ dbt_freshness                  │
│          └ moastock_eval_weekly (일 16시) ──▶ Kubeflow Pipelines                  │
│  alloy ─▶ Grafana Cloud (지표·로그)   dcgm-exporter (GPU 지표)                      │
│ ns kubeflow: Kubeflow Pipelines 2.16 (moastock-rag-eval: run_eval ▶ gate)          │
└──────────────────────────────────────────────────────────────────────────────────┘
Vertex AI Pipelines (관리형 Kubeflow): moastock-e2e-probe, 토 17시 - 바깥에서 사이트 API 로 전 경로 점검
```

| 무엇 | 어디 | 비고 |
|---|---|---|
| 서빙 매니페스트 | `serving.yaml` | 서비스 이름·포트는 Compose 때와 같다 |
| GPU 장치 플러그인 | `base.yaml` | `nvidia.com/gpu` 4개로 시간 분할. Docker 기본 런타임이 nvidia |
| Airflow 3.1 | `airflow.yaml`, `deploy/airflow/dags/` | LocalExecutor, 작업은 KubernetesPodOperator 파드 |
| dbt | `analytics/` | 분석 모델(analytics.*) + 데이터 품질 테스트 |
| Spark | `spark/` | PySpark local 모드, 종목 동시 언급 → spark.news_comention |
| Kubeflow Pipelines | `install-kfp.sh`, `pipelines/rag_eval.py` | 평가 회차 → eval.runs, 회귀 시 실패 |
| Vertex AI | `pipelines/vertex.sh` | 주간 일정 `moastock-e2e-probe-weekly` |
| 관측성 | `observability.yaml`, `setup-observability.sh`, `assistant/observability.py` | 키 파일을 채우면 켜진다 |

## 자주 하는 일 (VM 에서)

```bash
cd ~/moastock
bash deploy/k8s/deploy.sh serving     # 오케스트레이터 코드 바꾼 뒤 (이미지 빌드 → 재시작)
bash deploy/k8s/deploy.sh airflow     # DAG·dbt·Spark·파이프라인 바꾼 뒤
kubectl -n moastock get pods
kubectl -n moastock logs deploy/assistant -c assistant --tail=50

# 화면 (노트북에서): gcloud compute ssh moastock-ai --zone asia-northeast3-b -- -L 8080:localhost:8080 -L 8081:localhost:8081
kubectl -n moastock port-forward svc/airflow 8080            # Airflow
kubectl -n kubeflow port-forward svc/ml-pipeline-ui 8081:80   # Kubeflow Pipelines

# 수동 실행
kubectl -n moastock exec deploy/airflow -c scheduler -- airflow dags trigger moastock_daily
kubectl -n moastock exec deploy/airflow -c scheduler -- airflow dags trigger moastock_eval_weekly -c '{"limit": 3}'
bash deploy/gcp/daily.sh               # Airflow 없이 수집만 (예전 cron 방식)

# 분석 표 보기
kubectl -n moastock exec deploy/db -- psql -U moarag -c "select * from analytics.ingest_daily order by load_day desc limit 10"
```

## 처음 설치 (새 VM)

1. `sudo bash deploy/gcp/setup-vm.sh <버킷>` - Docker·NVIDIA 도구, 데이터 받기
2. `deploy/gcp/.env` 에 `ASSIST_SHARED_SECRET`, `TUNNEL_TOKEN`, `DART_API_KEY`
3. `sudo bash deploy/k8s/install-k3s.sh` → `bash deploy/k8s/install-kfp.sh` → `bash deploy/k8s/deploy.sh all`
4. 관측성: `deploy/gcp/observability.env` 채우고 `bash deploy/k8s/setup-observability.sh`

## 메모

- 원천 표(doc·chunk·kg_edge)는 수집 코드가 통째로 바꿔 끼우기도 해서 dbt 는 그 위에 뷰를 만들지 않는다 (staging=ephemeral).
- 노드 하나라 CPU **예약(requests)** 을 작게 잡았다. 실제 사용 상한(limits)은 메모리만 둔다.
- 스팟 회수 → VM 재시작 → k3s·파드 자동 복구. 돌던 Airflow 작업은 재시도(2회)로 이어진다.
- 메모리(26GB): vLLM ~5GB, Postgres ~4GB, Kubeflow ~3GB, Airflow ~1.5GB, 수집 작업 파드 최대 6GB.
