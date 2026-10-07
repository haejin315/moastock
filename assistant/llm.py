"""언어 모델 호출 (OpenAI 호환). 로컬 llama.cpp와 클라우드 vLLM 모두 같은 코드로 부른다.

Qwen3.5는 답하기 전에 길게 추론하는 '생각하기' 모드가 있다. 빠른 단계(질문 해석)는 끄고,
답변 단계도 기본은 끈다(작은 모델·CPU에서 지연이 크게 늘기 때문). 필요하면 think=True로.
"""
from __future__ import annotations

import json
import re

from openai import OpenAI

from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

_client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, timeout=600)


def _extra(think: bool) -> dict:
    # llama.cpp·vLLM 모두 chat_template_kwargs 로 Qwen의 생각하기 모드를 켜고 끈다
    return {"chat_template_kwargs": {"enable_thinking": think}}


_THINK = re.compile(r"<think>[\s\S]*?</think>\s*", re.I)


def complete(messages, *, max_tokens=400, temperature=0.2, think=False) -> str:
    r = _client.chat.completions.create(model=LLM_MODEL, messages=messages, max_tokens=max_tokens,
                                        temperature=temperature, extra_body=_extra(think))
    return _THINK.sub("", r.choices[0].message.content or "").strip()


def complete_json(messages, *, max_tokens=300) -> dict:
    """JSON만 받는다. 모델이 앞뒤에 말을 붙여도 첫 JSON 객체만 꺼낸다."""
    text = complete(messages, max_tokens=max_tokens, temperature=0)
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def stream(messages, *, max_tokens=700, temperature=0.2, think=False):
    """답변 토큰을 하나씩 낸다 (생각하기 구간은 걸러낸다)."""
    r = _client.chat.completions.create(model=LLM_MODEL, messages=messages, max_tokens=max_tokens,
                                        temperature=temperature, stream=True, extra_body=_extra(think))
    in_think = False
    for chunk in r:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        piece = getattr(delta, "content", None) or ""
        if not piece:
            continue
        if "<think>" in piece:
            in_think = True
        if in_think:
            if "</think>" in piece:
                in_think = False
                piece = piece.split("</think>", 1)[1]
            else:
                continue
        if piece:
            yield piece
