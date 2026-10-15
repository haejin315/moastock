#!/usr/bin/env bash
# 코드가 바뀌면 VM 에서 실행: 앱 이미지 빌드 → 비밀값 갱신 → 매니페스트 적용 → 오케스트레이터 다시 띄우기.
#   bash deploy/k8s/deploy.sh [serving|airflow|all]
set -euo pipefail
cd "$(dirname "$0")/../.."
what=${1:-all}
TAG=$(date +%Y%m%d%H%M)

build() {   # 이름 Dockerfile [context]
  sudo docker build -q -t "moastock/$1:$TAG" -t "moastock/$1:current" -f "$2" "${3:-.}" >/dev/null
  echo "이미지 moastock/$1:$TAG"
}

# 비밀값: VM 의 deploy/gcp/.env (저장소에 없음) → 쿠버네티스 Secret
kubectl -n moastock create secret generic moastock-env --from-env-file=deploy/gcp/.env \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

if [[ $what == serving || $what == all ]]; then
  build assistant deploy/gcp/Dockerfile.assistant
  kubectl apply -f deploy/k8s/serving.yaml
  kubectl -n moastock rollout restart deploy/assistant
fi
if [[ $what == airflow || $what == all ]]; then
  build dbt analytics/Dockerfile analytics
  build spark spark/Dockerfile spark
  # Airflow 비밀값(JWT·세션·Fernet 키)은 처음 한 번 만들어 두고 그대로 쓴다
  if ! kubectl -n moastock get secret airflow-secrets >/dev/null 2>&1; then
    kubectl -n moastock create secret generic airflow-secrets \
      --from-literal=AIRFLOW__API_AUTH__JWT_SECRET="$(openssl rand -hex 32)" \
      --from-literal=AIRFLOW__API__SECRET_KEY="$(openssl rand -hex 32)" \
      --from-literal=AIRFLOW__CORE__FERNET_KEY="$(openssl rand -base64 32 | tr '+/' '-_')"
  fi
  sudo mkdir -p /data/airflow/logs && sudo chown -R 50000:0 /data/airflow
  # Kubeflow 파이프라인 컴파일 → Airflow 가 읽는 자리로
  sudo docker run --rm -v "$PWD/pipelines:/p" python:3.12-slim sh -c \
    "pip install -q --root-user-action=ignore kfp==2.16.0 kfp-kubernetes >/dev/null 2>&1; python /p/rag_eval.py"
  mkdir -p deploy/airflow/dags/pipelines && cp pipelines/compiled/*.yaml deploy/airflow/dags/pipelines/
  kubectl apply -f deploy/k8s/airflow.yaml
  kubectl -n moastock rollout restart deploy/airflow
fi
kubectl -n moastock get pods
