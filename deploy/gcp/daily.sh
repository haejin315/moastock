#!/usr/bin/env bash
# 매일 증분: 새 뉴스·공시 수집·적재 → 회사 관계 그래프 다시 만들기.
# VM crontab (VM 시간대는 UTC): 15:20·21:00 KST = 06:20·12:00 UTC
#   20 6 * * *  bash ~/moastock/deploy/gcp/daily.sh
#   0 12 * * *  bash ~/moastock/deploy/gcp/daily.sh
set -u
cd "$(dirname "$0")"
# /data/rag 는 root 소유라 로그 폴더는 sudo 로 만들고 cron 사용자에게 넘긴다
[ -w /data/rag/daily ] || { sudo mkdir -p /data/rag/daily && sudo chown "$(id -u):$(id -g)" /data/rag/daily; }
exec >> /data/rag/daily/cron.log 2>&1
exec 9>/tmp/moastock-daily.lock
flock -n 9 || { echo "$(date -Is) 이전 실행이 아직 도는 중 - 건너뜀"; exit 0; }
echo "== $(date -Is) 시작"
RUN="sudo docker compose exec -T -e MOARAG_DATA_DIR=/data/rag/daily -w /app/rag assistant python -m"
$RUN moarag.daily && $RUN moarag.kg_build
echo "== $(date -Is) 끝 (exit $?)"
