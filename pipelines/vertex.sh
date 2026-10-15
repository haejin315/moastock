#!/usr/bin/env bash
# Vertex AI Pipelines (관리형 Kubeflow): 바깥에서 보는 E2E 점검 파이프라인 실행·주간 일정 등록.
#   python pipelines/rag_eval.py            (먼저 컴파일 → pipelines/compiled/e2e_probe.json, kfp SDK 필요)
#   bash pipelines/vertex.sh run            지금 한 번 실행 (AI 비서 운영 시간 15~22시 안에)
#   bash pipelines/vertex.sh schedule       매주 토요일 17시(KST) 일정 등록
#   bash pipelines/vertex.sh list           일정·최근 실행 보기
# 비용: 실행 1회 $0.03 + 컴포넌트 VM 몇 분. 실행 기록·지표는 콘솔 Vertex AI > Pipelines 에서 본다.
set -euo pipefail
PROJECT=moastock-ai-286; REGION=asia-northeast3
API="https://$REGION-aiplatform.googleapis.com/v1/projects/$PROJECT/locations/$REGION"
ROOT=gs://moastock-ai-286-data/vertex-pipelines
SPEC="$(dirname "$0")/compiled/e2e_probe.json"
TOKEN=$(gcloud auth print-access-token)
job() {  # 표시 이름 → pipelineJob JSON
  python -c "import json,sys; print(json.dumps({'displayName': sys.argv[1], 'pipelineSpec': json.load(open(sys.argv[2], encoding='utf-8')),
    'runtimeConfig': {'gcsOutputDirectory': '$ROOT'}, 'labels': {'app': 'moastock'}}, ensure_ascii=False))" "$1" "$SPEC"
}
call() { curl -s -X "$1" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json; charset=utf-8" "${@:3}" "$API/$2"; }
case "${1:-}" in
  run)      job moastock-e2e-probe-manual | call POST pipelineJobs --data-binary @- ;;
  schedule) python -c "import json,sys; print(json.dumps({'displayName': 'moastock-e2e-probe-weekly', 'cron': 'TZ=Asia/Seoul 0 17 * * 6',
              'maxConcurrentRunCount': '1', 'createPipelineJobRequest': {'parent': 'projects/$PROJECT/locations/$REGION',
              'pipelineJob': json.loads(sys.stdin.read())}}, ensure_ascii=False))" < <(job moastock-e2e-probe) \
              | call POST schedules --data-binary @- ;;
  list)     call GET schedules; call GET "pipelineJobs?pageSize=5" ;;
  *)        sed -n 2,7p "$0"; exit 1 ;;
esac
