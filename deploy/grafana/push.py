"""Grafana Cloud 에 'moastock 운영' 대시보드와 알림 규칙을 올린다 (여러 번 실행해도 같은 것을 덮어쓴다).

  python deploy/grafana/push.py            (저장소 루트에서, 표준 라이브러리만 쓴다)

키는 deploy/gcp/observability.env 에서 읽는다:
  GRAFANA_URL=https://<스택>.grafana.net
  GRAFANA_SA_TOKEN=glsa_...     (Grafana 서비스 계정 토큰, 역할 Editor 이상 - 데이터 쓰기용 GRAFANA_API_TOKEN 과 다르다)

알림은 VM 이 켜져 있을 때(15~22시)만 의미가 있다. VM 이 꺼지면 지표가 끊기므로 '자료 없음'은 정상(OK)으로 둔다.
알림을 받을 곳(이메일·웹훅)은 정하지 않았다 - Grafana 의 Alerting 화면에서 상태를 본다.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FOLDER = ("moastock", "moastock")              # (uid, 이름)
DASH_UID = "moastock-ops"
GROUP = "moastock"


def env() -> dict:
    out = {}
    for line in (ROOT / "deploy/gcp/observability.env").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Z_]+)=[\"']?(.*?)[\"']?\s*$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


class Grafana:
    def __init__(self, url: str, token: str):
        self.url, self.token = url.rstrip("/"), token

    def call(self, method: str, path: str, body=None, ok404=False):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                                              "X-Disable-Provenance": "true"})   # 화면에서도 고칠 수 있게
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read().decode()
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as e:
            if ok404 and e.code == 404:
                return None
            sys.exit(f"{method} {path} → HTTP {e.code}: {e.read().decode()[:300]}")


# ---- 대시보드 ------------------------------------------------------------------------

PROM = {"type": "prometheus", "uid": "${prom}"}
LOKI = {"type": "loki", "uid": "${loki}"}


def panel(title, exprs, x, y, w=8, h=8, kind="timeseries", unit=None, desc="", legend=None, extra=None):
    targets = [{"refId": chr(65 + i), "expr": e, "legendFormat": (legend or [None] * len(exprs))[i] or "__auto",
                "datasource": PROM} for i, e in enumerate(exprs)]
    p = {"type": kind, "title": title, "description": desc, "datasource": PROM, "targets": targets,
         "gridPos": {"x": x, "y": y, "w": w, "h": h},
         "fieldConfig": {"defaults": {"unit": unit} if unit else {}, "overrides": []}, "options": {}}
    if kind == "stat":
        p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value", "graphMode": "none"}
    if extra:
        p.update(extra)
    return p


def row(title, y):
    return {"type": "row", "title": title, "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []}


def q(level, metric, window="10m"):
    return f"histogram_quantile({level}, sum by (le) (rate({metric}_bucket[{window}])))"


def dashboard() -> dict:
    ps = [row("AI 비서 (사이트 챗봇)", 0),
          panel("질문 수", ["sum(increase(moastock_ask_total[$__range]))"], 0, 1, 4, 4, "stat", "short",
                "선택한 기간의 질문 수"),
          panel("실패율", ["sum(increase(moastock_ask_total{outcome=\"error\"}[$__range])) / "
                         "sum(increase(moastock_ask_total[$__range]))"], 4, 1, 4, 4, "stat", "percentunit",
                "오류로 답하지 못한 비율"),
          panel("숫자 근거율", ["sum(increase(moastock_answer_numbers_supported_total[$__range])) / "
                             "sum(increase(moastock_answer_numbers_total[$__range]))"], 8, 1, 4, 4, "stat",
                "percentunit", "답변 숫자 중 근거 원문의 값과 맞는 비율"),
          panel("첫 글자까지 (중앙값)", [q(0.5, "moastock_ask_first_token_seconds", "$__range")], 12, 1, 4, 4, "stat",
                "s", "질문하고 답변 첫 글자가 나올 때까지"),
          panel("전체 응답 (95%)", [q(0.95, "moastock_ask_seconds", "$__range")], 16, 1, 4, 4, "stat", "s"),
          panel("출처 이름 바로잡음", ["sum(increase(moastock_attribution_fixed_total[$__range]))"], 20, 1, 4, 4, "stat",
                "short", "인용 번호와 다른 언론사 이름을 코드가 고친 횟수 - 늘면 모델 품질을 의심"),
          panel("질문 결과", ["sum by (outcome) (increase(moastock_ask_total[5m]))"], 0, 5, 8, 8, unit="short",
                legend=["{{outcome}}"], desc="ok 정상 · error 오류 · declined 권유 거절·범위 밖 · no_evidence 자료 없음"),
          panel("응답 시간", [q(0.5, "moastock_ask_first_token_seconds"), q(0.95, "moastock_ask_first_token_seconds"),
                          q(0.5, "moastock_ask_seconds"), q(0.95, "moastock_ask_seconds")], 8, 5, 8, 8, unit="s",
                legend=["첫 글자 50%", "첫 글자 95%", "전체 50%", "전체 95%"]),
          panel("질문 종류", ["sum by (intent) (increase(moastock_ask_intent_total[$__range]))"], 16, 5, 8, 8,
                "barchart", "short", legend=["{{intent}}"],
                extra={"targets": [{"refId": "A", "datasource": PROM, "instant": True, "format": "table",
                                    "expr": "sum by (intent) (increase(moastock_ask_intent_total[$__range]))"}],
                       "options": {"xField": "intent", "orientation": "horizontal"}}),

          row("언어 모델·GPU (T4)", 13),
          panel("GPU 사용률", ["DCGM_FI_DEV_GPU_UTIL"], 0, 14, 6, 7, unit="percent", legend=["GPU"]),
          panel("GPU 메모리", ["DCGM_FI_DEV_FB_USED * 1024 * 1024", "(DCGM_FI_DEV_FB_USED + DCGM_FI_DEV_FB_FREE) * 1024 * 1024"],
                6, 14, 6, 7, unit="bytes", legend=["사용", "전체"]),
          panel("GPU 온도·전력", ["DCGM_FI_DEV_GPU_TEMP", "DCGM_FI_DEV_POWER_USAGE"], 12, 14, 6, 7,
                legend=["온도 ℃", "전력 W"]),
          panel("vLLM 요청", ["sum(vllm:num_requests_running)", "sum(vllm:num_requests_waiting)"], 18, 14, 6, 7,
                unit="short", legend=["처리 중", "대기"]),
          panel("생성 속도", ["sum(rate(vllm:generation_tokens_total[1m]))", "sum(rate(vllm:prompt_tokens_total[1m]))"],
                0, 21, 8, 7, unit="short", legend=["생성 토큰/초", "입력 토큰/초"]),
          panel("KV 캐시 사용", ["max(vllm:kv_cache_usage_perc)"], 8, 21, 8, 7, unit="percentunit", legend=["KV 캐시"]),
          panel("vLLM 첫 토큰 (모델만)", [q(0.5, "vllm:time_to_first_token_seconds"),
                                     q(0.95, "vllm:time_to_first_token_seconds")], 16, 21, 8, 7, unit="s",
                legend=["50%", "95%"]),

          row("수집 (뉴스·공시)", 28),
          panel("최근 24시간 문서", ["max by (source) (moastock_docs_24h)"], 0, 29, 8, 6, "stat", "short",
                legend=["{{source}}"]),
          panel("가장 최근 문서 나이", ["max by (source) (moastock_doc_latest_age_seconds)"], 8, 29, 8, 6, "stat", "s",
                "뉴스가 20시간 넘게 멈추면 알림", legend=["{{source}}"]),
          panel("문서 나이 추이", ["max by (source) (moastock_doc_latest_age_seconds) / 3600"], 16, 29, 8, 6, unit="h",
                legend=["{{source}}"]),

          row("서버 (VM·컨테이너)", 35),
          panel("CPU", ["1 - avg(rate(node_cpu_seconds_total{mode=\"idle\"}[5m]))"], 0, 36, 6, 7, unit="percentunit",
                legend=["CPU"]),
          panel("메모리", ["1 - sum(node_memory_MemAvailable_bytes) / sum(node_memory_MemTotal_bytes)"], 6, 36, 6, 7,
                unit="percentunit", legend=["메모리"]),
          panel("디스크", ["1 - min(node_filesystem_avail_bytes{mountpoint=~\"/|/data\"} / "
                         "node_filesystem_size_bytes{mountpoint=~\"/|/data\"}) by (mountpoint)"], 12, 36, 6, 7,
                unit="percentunit", legend=["{{mountpoint}}"]),
          panel("컨테이너 메모리", ["topk(8, sum by (pod) (container_memory_working_set_bytes{namespace=\"moastock\", container!=\"\"}))"],
                18, 36, 6, 7, unit="bytes", legend=["{{pod}}"]),

          row("로그", 43),
          {"type": "logs", "title": "오류 로그 (moastock 파드)", "datasource": LOKI, "gridPos": {"x": 0, "y": 44, "w": 24, "h": 10},
           "targets": [{"refId": "A", "datasource": LOKI,
                        "expr": "{namespace=\"moastock\"} |~ \"(?i)traceback|error|실패\" !~ \"GET /health\""}],
           "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending"}}]
    return {"uid": DASH_UID, "title": "moastock 운영", "tags": ["moastock"], "timezone": "Asia/Seoul",
            "time": {"from": "now-12h", "to": "now"}, "refresh": "1m", "schemaVersion": 39, "panels": ps,
            "templating": {"list": [
                {"name": "prom", "label": "지표", "type": "datasource", "query": "prometheus", "hide": 2},
                {"name": "loki", "label": "로그", "type": "datasource", "query": "loki", "hide": 2}]}}


# ---- 알림 규칙 -----------------------------------------------------------------------

def rule(uid, title, expr, op, value, for_, summary, prom_uid, window=900):
    return {"uid": uid, "title": title, "folderUID": FOLDER[0], "ruleGroup": GROUP, "condition": "C", "for": for_,
            "noDataState": "OK", "execErrState": "OK", "labels": {"service": "moastock"},
            "annotations": {"summary": summary},
            "data": [{"refId": "A", "relativeTimeRange": {"from": window, "to": 0}, "datasourceUid": prom_uid,
                      "model": {"refId": "A", "expr": expr, "instant": True}},
                     {"refId": "C", "relativeTimeRange": {"from": 0, "to": 0}, "datasourceUid": "__expr__",
                      "model": {"refId": "C", "type": "threshold", "expression": "A",
                                "conditions": [{"evaluator": {"type": op, "params": [value]}}]}}]}


def rules(prom_uid: str) -> list[dict]:
    return [
        rule("moastock-ask-errors", "챗봇 실패율 20% 초과",
             "(sum(increase(moastock_ask_total{outcome=\"error\"}[15m])) / sum(increase(moastock_ask_total[15m])))"
             " and on() (sum(increase(moastock_ask_total[15m])) >= 3)",
             "gt", 0.2, "5m", "최근 15분 질문 중 20% 넘게 오류로 끝났습니다 (질문 3개 이상일 때만 봅니다).", prom_uid),
        rule("moastock-ask-slow", "챗봇 첫 글자 95%가 30초 초과",
             "histogram_quantile(0.95, sum by (le) (rate(moastock_ask_first_token_seconds_bucket[15m])))",
             "gt", 30, "15m", "사용자가 첫 글자를 보기까지 30초 넘게 걸립니다 - GPU·vLLM 대기열을 확인하세요.", prom_uid),
        rule("moastock-llm-down", "언어 모델 서버(vLLM) 응답 없음", "max(up{app=\"llm\"})",
             "lt", 1, "5m", "vLLM 지표를 5분 넘게 못 읽었습니다 - llm 파드가 죽었거나 GPU 문제입니다.", prom_uid, 300),
        rule("moastock-ingest-stalled", "뉴스 수집 20시간 넘게 멈춤",
             "max(moastock_doc_latest_age_seconds{source=\"news\"}) / 3600",
             "gt", 20, "30m", "가장 최근 뉴스가 20시간 넘게 오래됐습니다 - Airflow moastock_daily 수집을 확인하세요.", prom_uid),
    ]


def main():
    e = env()
    url, token = e.get("GRAFANA_URL"), e.get("GRAFANA_SA_TOKEN")
    if not url or not token:
        sys.exit("deploy/gcp/observability.env 에 GRAFANA_URL 과 GRAFANA_SA_TOKEN 을 채워 주세요")
    g = Grafana(url, token)
    ds = g.call("GET", "/api/datasources")
    def pick(kind, suffix):
        c = [d for d in ds if d["type"] == kind and d["name"].endswith(suffix)] or [d for d in ds if d["type"] == kind]
        return c[0]["uid"] if c else None
    prom, loki = pick("prometheus", "-prom"), pick("loki", "-logs")
    print(f"데이터 소스: 지표 {prom}, 로그 {loki}")

    if not g.call("GET", f"/api/folders/{FOLDER[0]}", ok404=True):
        g.call("POST", "/api/folders", {"uid": FOLDER[0], "title": FOLDER[1]})
    d = dashboard()
    for v in d["templating"]["list"]:
        v["current"] = {"value": prom if v["name"] == "prom" else loki}
    r = g.call("POST", "/api/dashboards/db", {"dashboard": d, "folderUid": FOLDER[0], "overwrite": True,
                                             "message": "push.py"})
    print(f"대시보드: {url.rstrip('/')}{r.get('url', '')}")

    for rl in rules(prom):
        if g.call("GET", f"/api/v1/provisioning/alert-rules/{rl['uid']}", ok404=True):
            g.call("PUT", f"/api/v1/provisioning/alert-rules/{rl['uid']}", rl)
        else:
            g.call("POST", "/api/v1/provisioning/alert-rules", rl)
        print(f"알림 규칙: {rl['title']}")
    g.call("PUT", f"/api/v1/provisioning/folder/{FOLDER[0]}/rule-groups/{GROUP}",
           {"title": GROUP, "interval": 60, "rules": [g.call("GET", f"/api/v1/provisioning/alert-rules/{r['uid']}")
                                                       for r in rules(prom)]})
    print("평가 주기 1분")


if __name__ == "__main__":
    main()
