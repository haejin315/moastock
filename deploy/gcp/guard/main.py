"""AI 비서 GPU 서버 지킴이 (Cloud Run 함수, Pub/Sub 토픽 moastock-ai-guard 로 깨어난다).

두 가지 메시지를 받는다.
1. 예산 알림 (Cloud Billing 예산이 하루 여러 번 보냄, attributes 에 budgetId)
   - 이번 달 사용액 ≥ 예산(월 $100): 자동 켜기 일정을 떼고 VM을 끈다 → 이번 달은 더 켜지지 않는다
   - 새 달이 되어 사용액이 예산의 절반 아래로 내려왔는데 일정이 떼어져 있으면: 일정을 다시 붙인다
2. 운영 시간 점검 (Cloud Scheduler, 운영 시간 중 10분마다 {"action": "tick"})
   - 일정이 붙어 있는데(= 예산 정지 상태가 아님) VM이 꺼져 있으면(스팟 회수) 다시 켠다
"""
import base64
import json
from datetime import datetime, timedelta, timezone

import functions_framework
from google.cloud import compute_v1

PROJECT, REGION, ZONE, VM = "moastock-ai-286", "asia-northeast3", "asia-northeast3-b", "moastock-ai"
POLICY = f"projects/{PROJECT}/regions/{REGION}/resourcePolicies/moastock-ai-hours"
OPEN_HOURS = (15, 22)                      # KST, 사이트 Worker 의 ASSIST_HOURS 와 같게
KST = timezone(timedelta(hours=9))

instances = compute_v1.InstancesClient()


def _vm():
    return instances.get(project=PROJECT, zone=ZONE, instance=VM)


def _scheduled(vm) -> bool:
    return any(p.rstrip("/").endswith("/moastock-ai-hours") for p in vm.resource_policies)


def _on_budget(data: dict):
    cost, budget = float(data.get("costAmount", 0)), float(data.get("budgetAmount", 0))
    vm = _vm()
    cur = data.get("currencyCode", "")
    print(f"[budget] {cost:,.0f}/{budget:,.0f} {cur}, vm={vm.status}, scheduled={_scheduled(vm)}")
    if budget and cost >= budget:
        if _scheduled(vm):
            instances.remove_resource_policies(
                project=PROJECT, zone=ZONE, instance=VM,
                instances_remove_resource_policies_request_resource=compute_v1.InstancesRemoveResourcePoliciesRequest(
                    resource_policies=[POLICY])).result()
            print("[budget] 예산 도달 - 자동 켜기 일정 해제")
        if vm.status in ("RUNNING", "PROVISIONING", "STAGING"):
            instances.stop(project=PROJECT, zone=ZONE, instance=VM).result()
            print("[budget] 예산 도달 - VM 정지")
    elif budget and cost < budget * 0.5 and not _scheduled(vm):
        instances.add_resource_policies(
            project=PROJECT, zone=ZONE, instance=VM,
            instances_add_resource_policies_request_resource=compute_v1.InstancesAddResourcePoliciesRequest(
                resource_policies=[POLICY])).result()
        print("[budget] 새 달 - 자동 켜기 일정 복구")


def _on_tick():
    now = datetime.now(KST)
    if not (OPEN_HOURS[0] <= now.hour < OPEN_HOURS[1]) or now.hour == OPEN_HOURS[1] - 1 and now.minute >= 50:
        return                              # 운영 시간 밖, 또는 곧 꺼질 시각
    vm = _vm()
    if _scheduled(vm) and vm.status in ("TERMINATED", "STOPPED"):
        print(f"[tick] 운영 시간인데 VM이 꺼져 있음({vm.status}) - 다시 켠다")
        instances.start(project=PROJECT, zone=ZONE, instance=VM).result()


@functions_framework.cloud_event
def guard(event):
    msg = event.data.get("message", {})
    attrs = msg.get("attributes") or {}
    raw = base64.b64decode(msg.get("data", "")) if msg.get("data") else b"{}"
    data = json.loads(raw or b"{}")
    if "budgetId" in attrs:
        _on_budget(data)
    elif data.get("action") == "tick":
        _on_tick()
