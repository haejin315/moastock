"""관측성: Langfuse 추적. LANGFUSE_PUBLIC_KEY·LANGFUSE_SECRET_KEY(·LANGFUSE_HOST)가 있으면 켜지고, 없으면 아무 일도 하지 않는다.

질문 하나 = 트레이스 하나 (assistant.ask)
  ├ node.understand   질문 해석 (의도·종목·기간) ─ llm.complete_json (generation)
  ├ node.gather       자료 모으기 ─ retrieval.search ─ rerank / 도구 호출
  ├ node.answer       답변 생성 ─ llm.stream (generation)
  └ node.verify       숫자·인용 검증 → 트레이스 점수(number_support, has_citation, invalid_citations)
평가(eval/run_eval.py)도 같은 트레이스로 남고 태그 eval 이 붙는다.
"""
from __future__ import annotations

import os

ENABLED = bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"))
_lf = None
if ENABLED:
    try:
        from langfuse import get_client
        from langfuse import observe as _observe
        _lf = get_client()
    except Exception as e:                      # 패키지·설정 문제로 서비스가 멈추지 않게
        print(f"[observability] Langfuse 끔: {e!r}", flush=True)
        ENABLED = False


def observe(*args, **kwargs):
    """@observe / @observe(name=..., as_type="generation", capture_input=False …). 꺼져 있으면 함수를 그대로 돌려준다."""
    if ENABLED:
        return _observe(*args, **kwargs)
    if args and callable(args[0]) and len(args) == 1 and not kwargs:
        return args[0]
    return lambda f: f


def _safe(fn_name: str, **kw):
    if not ENABLED:
        return
    try:
        getattr(_lf, fn_name)(**kw)
    except Exception as e:
        print(f"[observability] {fn_name} 실패: {e!r}", flush=True)


def trace(**kw):
    """현재 트레이스 속성 (name, input, output, tags, metadata, session_id …)"""
    _safe("update_current_trace", **kw)


def span(**kw):
    """현재 구간 속성 (input, output, metadata, level, status_message …)"""
    _safe("update_current_span", **kw)


def generation(**kw):
    """현재 LLM 호출 속성 (model, usage_details, model_parameters …)"""
    _safe("update_current_generation", **kw)


def score(name: str, value: float, comment: str | None = None):
    _safe("score_current_trace", name=name, value=value, comment=comment)


def flush():
    _safe("flush")
