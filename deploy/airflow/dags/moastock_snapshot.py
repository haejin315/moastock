"""평일 장 마감 뒤 사이트 일일 스냅샷(GitHub Actions 'daily-snapshot')을 바로 실행시킨다. 16:10 KST.

GitHub 예약 실행(cron)은 7~10시간씩 늦게 돌아(2026-10~11 실측, 밤 11시~새벽 2시) 그날 밤까지 사이트 종가가
전날 값이었다. VM 은 이 시각에 켜져 있으므로 여기서 workflow_dispatch 로 바로 돌린다. GitHub 예약 실행은
VM 이 꺼진 날을 위한 예비로 남겨 둔다 (늦게 돌아도 바뀐 게 없으면 커밋하지 않는다).

토큰: Secret github / GITHUB_DISPATCH_TOKEN (deploy/k8s/setup-observability.sh 가 키 파일에서 만든다).
없으면 아무것도 하지 않고 성공으로 끝난다.
"""
from datetime import datetime

import pendulum
from airflow.sdk import DAG
from kubernetes.client import models as k8s

from moastock_common import DEFAULT_ARGS, pod

KST = pendulum.timezone("Asia/Seoul")
REPO, WORKFLOW = "haejin315/moastock", "snapshot.yml"
TOKEN = k8s.V1EnvVar(name="GITHUB_TOKEN", value_from=k8s.V1EnvVarSource(
    secret_key_ref=k8s.V1SecretKeySelector(name="github", key="GITHUB_DISPATCH_TOKEN", optional=True)))

DISPATCH = f"""
import os, sys, urllib.request, urllib.error
token = os.environ.get("GITHUB_TOKEN", "").strip()
if not token:
    print("GITHUB_DISPATCH_TOKEN 없음 - GitHub 예약 실행에 맡긴다"); sys.exit(0)
req = urllib.request.Request(
    "https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches", data=b'{{"ref": "main"}}',
    method="POST", headers={{"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                             "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "moastock-airflow"}})
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        print("daily-snapshot 실행 요청", r.status)
except urllib.error.HTTPError as e:
    print("실행 요청 실패", e.code, e.read()[:300]); sys.exit(1)
"""

with DAG(
    dag_id="moastock_snapshot",
    description="평일 16:10 사이트 일일 스냅샷(GitHub Actions) 실행",
    schedule="10 16 * * 1-5",
    start_date=datetime(2026, 11, 19, tzinfo=KST),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    tags=["moastock", "site"],
) as dag:
    pod("dispatch", "assistant", ["python", "-c", DISPATCH], env=[TOKEN], mem="256Mi", timeout_min=5)
