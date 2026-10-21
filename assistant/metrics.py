"""운영 지표 (Prometheus). Alloy 가 파드 주석으로 긁어 Grafana Cloud 로 보낸다 - 대시보드 'moastock 운영'과 알림의 재료.

  moastock_ask_total{outcome}             질문 수 (ok | error | declined(권유 거절·범위 밖) | no_evidence)
  moastock_ask_seconds                    질문 하나 전체 처리 시간
  moastock_ask_first_token_seconds        첫 답변 글자까지 걸린 시간 (사용자가 기다리는 시간)
  moastock_ask_intent_total{intent}       질문 종류
  moastock_tool_failures_total{tool}      도구 실패 (시세·지표·검색 등)
  moastock_answer_numbers_total / _supported_total   답변 숫자 중 근거와 맞는 것 (비율 = 숫자 근거율)
  moastock_attribution_fixed_total        인용 번호와 다른 언론사 이름을 바로잡은 횟수
  moastock_docs_24h{source}               최근 24시간에 들어온 문서 수 (news | dart) - 수집이 멈추면 0
  moastock_doc_latest_age_seconds{source} 가장 최근 문서가 얼마나 오래됐는지

공개 터널(127.0.0.1:8100)과 섞이지 않게 따로 9100 포트에서 내보낸다.
"""
from __future__ import annotations

import threading
import time

from prometheus_client import Counter, Histogram, start_http_server
from prometheus_client.core import REGISTRY, GaugeMetricFamily

PORT = 9100
BUCKETS = (1, 2, 3, 5, 8, 10, 15, 20, 30, 45, 60, 90, 120)

ASK = Counter("moastock_ask_total", "질문 수", ["outcome"])
ASK_SECONDS = Histogram("moastock_ask_seconds", "질문 전체 처리 시간", buckets=BUCKETS)
FIRST_TOKEN = Histogram("moastock_ask_first_token_seconds", "첫 답변 글자까지 시간", buckets=BUCKETS)
INTENT = Counter("moastock_ask_intent_total", "질문 종류", ["intent"])
TOOL_FAIL = Counter("moastock_tool_failures_total", "도구 실패", ["tool"])
NUMBERS = Counter("moastock_answer_numbers_total", "답변에서 검사한 숫자")
NUMBERS_OK = Counter("moastock_answer_numbers_supported_total", "근거와 맞는 숫자")
ATTR_FIXED = Counter("moastock_attribution_fixed_total", "바로잡은 언론사 이름")


class DocsCollector:
    """긁을 때마다 DB 에 묻지 않고 5분마다 한 번 센다 (30초 간격 수집에 비해 문서 수는 천천히 변한다)"""
    TTL = 300

    def __init__(self):
        self._at, self._rows, self._lock = 0.0, [], threading.Lock()

    def _rows_cached(self):
        with self._lock:
            if time.time() - self._at > self.TTL:
                from retrieval import conn
                self._rows = conn().execute(
                    "SELECT source_type, count(*) FILTER (WHERE published_at >= now() - interval '24 hours'), "
                    "extract(epoch FROM now() - max(published_at)) FROM doc "
                    "WHERE published_at >= now() - interval '30 days' GROUP BY source_type").fetchall()
                self._at = time.time()
            return self._rows

    def collect(self):
        docs = GaugeMetricFamily("moastock_docs_24h", "최근 24시간 문서 수", labels=["source"])
        age = GaugeMetricFamily("moastock_doc_latest_age_seconds", "가장 최근 문서의 나이", labels=["source"])
        try:
            for source, n, sec in self._rows_cached():
                docs.add_metric([source], n)
                age.add_metric([source], float(sec or 0))
        except Exception as e:                       # DB 가 잠깐 안 돼도 나머지 지표는 내보낸다
            print(f"[metrics] 문서 수 실패: {e!r}", flush=True)
        yield docs
        yield age


_started = False


def start():
    global _started
    if not _started:
        REGISTRY.register(DocsCollector())
        start_http_server(PORT)
        _started = True


def observe_done(checks: dict):
    """done 이벤트의 검사 결과 → 결과 종류·숫자 근거·출처 바로잡기"""
    if not checks:
        return "no_evidence"
    NUMBERS.inc(checks.get("numbers", 0))
    NUMBERS_OK.inc(checks.get("numbers_supported", 0))
    ATTR_FIXED.inc(checks.get("attribution_fixed", 0))
    return "ok"
