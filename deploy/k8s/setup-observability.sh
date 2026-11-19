#!/usr/bin/env bash
# 관측성 켜기: 키 파일 → 쿠버네티스 Secret → 수집기 배포 → assistant·Airflow 다시 띄우기 (Langfuse 추적·실패 알림이 켜진다).
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
#   GRAFANA_URL=https://<스택>.grafana.net             (대시보드·알림 올리기: python deploy/grafana/push.py)
#   GRAFANA_SA_TOKEN=glsa_...                          (Grafana 서비스 계정 토큰, Editor - 클러스터에는 넣지 않는다)
#   ALERT_WEBHOOK_URL=https://discord.com/api/webhooks/...   (Airflow 작업 실패 알림, Slack 수신 웹훅도 된다)
#   GITHUB_DISPATCH_TOKEN=github_pat_...               (일일 스냅샷 실행용. 세분화 토큰: moastock 저장소, Actions 읽기·쓰기만)
set -euo pipefail
cd "$(dirname "$0")/../.."
ENV=deploy/gcp/observability.env
[ -f "$ENV" ] || { echo "$ENV 가 없습니다 (위 주석의 키를 채워 주세요)"; exit 1; }
# 윈도우에서 편집한 파일(CRLF)·따옴표로 감싼 값도 받게 정리한 사본을 쓴다
CLEAN=$(mktemp); trap 'rm -f "$CLEAN"' EXIT
sed -e 's/\r$//' -e "s/^\([A-Z_]*\)=[\"']\(.*\)[\"']$/\1=\2/" "$ENV" > "$CLEAN"
ENV=$CLEAN

kubectl -n moastock create secret generic observability \
  --from-env-file=<(grep -E '^LANGFUSE_' "$ENV") --dry-run=client -o yaml | kubectl apply -f -
if grep -q '^GRAFANA_API_TOKEN=.' "$ENV"; then
  kubectl -n moastock create secret generic grafana-cloud \
    --from-env-file=<(grep -E '^GRAFANA_(PROM|LOKI|API)_' "$ENV") --dry-run=client -o yaml | kubectl apply -f -
  kubectl apply -f deploy/k8s/observability.yaml
  kubectl -n moastock rollout restart deploy/alloy
fi
# 웹훅 주소가 비어 있으면 빈 값으로 둔다 (Airflow 는 로그만 남긴다). 바뀌었을 때만 Airflow 를 다시 띄운다
if kubectl -n moastock create secret generic alerts --from-env-file=<(grep -E '^ALERT_WEBHOOK_URL=' "$ENV") \
     --dry-run=client -o yaml | kubectl apply -f - | grep -Eq "configured|created"; then
  kubectl -n moastock rollout restart deploy/airflow
fi
# 일일 스냅샷을 장 마감 직후 GitHub 에서 돌리게 하는 토큰 (DAG moastock_snapshot). 파드가 뜰 때 읽으므로 재시작은 필요 없다
if grep -q '^GITHUB_DISPATCH_TOKEN=.' "$ENV"; then
  kubectl -n moastock create secret generic github --from-env-file=<(grep -E '^GITHUB_DISPATCH_TOKEN=' "$ENV") \
    --dry-run=client -o yaml | kubectl apply -f -
fi
kubectl -n moastock rollout restart deploy/assistant
kubectl -n moastock rollout status deploy/assistant --timeout=180s
kubectl -n moastock exec deploy/assistant -c assistant -- python -c \
  "import observability as o; print('Langfuse', '켜짐' if o.ENABLED else '꺼짐')"
