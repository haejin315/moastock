#!/usr/bin/env bash
# 매일 증분: 새 뉴스·공시 수집·적재 → 회사 관계 그래프 다시 만들기.
# VM crontab (VM 시간대는 UTC): 15:20·21:00 KST = 06:20·12:00 UTC
#   20 6 * * *  bash ~/moastock/deploy/gcp/daily.sh
#   0 12 * * *  bash ~/moastock/deploy/gcp/daily.sh
#   @reboot sleep 600 && bash ~/moastock/deploy/gcp/daily.sh   (스팟 회수 뒤 다시 켜지면 끊긴 수집 이어서)
set -u
cd "$(dirname "$0")"
# /data/rag 는 root 소유라 로그 폴더는 sudo 로 만들고 cron 사용자에게 넘긴다
[ -w /data/rag/daily ] || { sudo mkdir -p /data/rag/daily && sudo chown "$(id -u):$(id -g)" /data/rag/daily; }
exec >> /data/rag/daily/cron.log 2>&1
exec 9>/tmp/moastock-daily.lock
flock -n 9 || { echo "$(date -Is) 이전 실행이 아직 도는 중 - 건너뜀"; exit 0; }
echo "== $(date -Is) 시작"
# k3s 로 옮긴 뒤: 오케스트레이터 파드 안에서 실행 (Airflow DAG moastock_daily 가 이 스크립트를 대신한다 - 수동 실행용)
run() { kubectl -n moastock exec deploy/assistant -c assistant -- sh -c "cd /app/rag && MOARAG_DATA_DIR=/data/rag/daily python -m $1"; }
run moarag.daily && run moarag.kg_build
rc=$?
echo "== $(date -Is) 끝 (exit $rc)"
