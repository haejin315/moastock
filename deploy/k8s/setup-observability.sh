#!/usr/bin/env bash
# 관측성 켜기: 키 파일 → 쿠버네티스 Secret → 수집기 배포 → assistant 다시 띄우기 (Langfuse 추적이 켜진다).
#   bash deploy/k8s/setup-observability.sh            (VM 에서, 키 파일을 먼저 채운다)
#
# 키 파일 deploy/gcp/observability.env (저장소에 올리지 않는다 - .gitignore):
#   LANGFUSE_PUBLIC_KEY=pk-lf-...
#   LANGFUSE_SECRET_KEY=sk-lf-...
#   LANGFUSE_HOST=https://cloud.langfuse.com          (US 리전이면 https://us.cloud.langfuse.com)
#   GRAFANA_PROM_URL=https://prometheus-prod-XX-....grafana.net/api/prom/push
#   GRAFANA_PROM_USER=1234567                          (Prometheus 인스턴스 ID)
#   GRAFANA_LOKI_URL=https://logs-prod-XXX.grafana.net/loki/api/v1/push
#   GRAFANA_LOKI_USER=7654321                          (Loki 인스턴스 ID)
#   GRAFANA_API_TOKEN=glc_...                          (Access Policy 토큰: metrics:write, logs:write)
set -euo pipefail
cd "$(dirname "$0")/../.."
ENV=deploy/gcp/observability.env
[ -f "$ENV" ] || { echo "$ENV 가 없습니다 (위 주석의 키를 채워 주세요)"; exit 1; }

kubectl -n moastock create secret generic observability \
  --from-env-file=<(grep -E '^LANGFUSE_' "$ENV") --dry-run=client -o yaml | kubectl apply -f -
if grep -q '^GRAFANA_API_TOKEN=.' "$ENV"; then
  kubectl -n moastock create secret generic grafana-cloud \
    --from-env-file=<(grep -E '^GRAFANA_' "$ENV") --dry-run=client -o yaml | kubectl apply -f -
  kubectl apply -f deploy/k8s/observability.yaml
  kubectl -n moastock rollout restart deploy/alloy
fi
kubectl -n moastock rollout restart deploy/assistant
kubectl -n moastock rollout status deploy/assistant --timeout=180s
kubectl -n moastock exec deploy/assistant -c assistant -- python -c \
  "import observability as o; print('Langfuse', '켜짐' if o.ENABLED else '꺼짐')"
