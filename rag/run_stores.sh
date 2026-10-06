#!/usr/bin/env bash
# 저장소를 하나씩 띄워 적재 → 성능 측정 → 정지한다.
# 세 컨테이너를 동시에 띄우면 메모리(Docker VM)가 부족하고 서로 CPU를 다퉈 측정이 왜곡되므로,
# 측정 대상 하나만 실행 중인 상태에서 잰다. 볼륨은 남으므로 나중에 bench만 다시 돌릴 수 있다.
#
#   bash rag/run_stores.sh                     # 세 저장소 모두 (적재 + 측정)
#   bash rag/run_stores.sh qdrant              # 하나만
#   BENCH_ONLY=1 bash rag/run_stores.sh        # 적재 없이 측정만 다시
set -euo pipefail
cd "$(dirname "$0")"
PY="${PY:-D:/moastock-rag/.venv/Scripts/python.exe}"
STORES=("${@:-pgvector qdrant opensearch}")
read -r -a STORES <<< "${STORES[*]}"

declare -A SERVICE=([pgvector]=postgres [qdrant]=qdrant [opensearch]=opensearch)

wait_ready() {
  local svc=$1
  for _ in $(seq 1 120); do
    case $svc in
      postgres)   docker compose exec -T postgres pg_isready -U moarag >/dev/null 2>&1 && return 0 ;;
      qdrant)     curl -sf http://127.0.0.1:6333/readyz >/dev/null && return 0 ;;
      opensearch) curl -sf "http://127.0.0.1:9200/_cluster/health?wait_for_status=yellow&timeout=2s" >/dev/null && return 0 ;;
    esac
    sleep 3
  done
  echo "준비 시간 초과: $svc" >&2; return 1
}

for store in "${STORES[@]}"; do
  svc=${SERVICE[$store]}
  echo "=== $store ($svc) $(date '+%F %T')"
  docker compose stop >/dev/null 2>&1 || true          # 다른 저장소는 모두 정지
  docker compose up -d "$svc"
  wait_ready "$svc"
  if [ -z "${BENCH_ONLY:-}" ]; then
    "$PY" -m moarag.load "$store" --batch 5000
  fi
  "$PY" -m moarag.bench --stores "$store" ${BENCH_EF:+--ef "$BENCH_EF"}
  docker stats --no-stream --format "{{.Name}} 메모리 {{.MemUsage}}" | grep "$svc" || true
  docker compose stop "$svc"
done
"$PY" -m moarag.report
