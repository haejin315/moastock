"""AI 비서 API 서버 (FastAPI). 사이트 Worker의 /api/assistant 가 이 서버로 중계한다.

  POST /v1/ask   {"question": "..."}  →  text/event-stream
     event: status   {"text": "관련 뉴스·공시를 찾는 중"}
     event: plan     {"intent", "stocks", "days", "query"}
     event: evidence {"items": [출처…]}
     event: token    {"text": "답변 조각"}
     event: done     {"checks", "notes", "disclaimer", "sources"}
     event: error    {"message"}
  GET  /health

로컬:  python -m uvicorn server:app --port 8100   (assistant/ 에서)
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import metrics
import observability as obs
from graph import GRAPH

app = FastAPI(title="moastock assistant")
metrics.start()                                                  # :9100/metrics (Grafana 대시보드·알림)
SHARED_SECRET = os.environ.get("ASSIST_SHARED_SECRET", "")     # Worker ↔ 이 서버 사이 인증 (운영 필수)


class Ask(BaseModel):
    question: str = Field(min_length=2, max_length=500)


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/v1/ask")
async def ask(body: Ask, x_assist_secret: str | None = Header(default=None)):
    if SHARED_SECRET and x_assist_secret != SHARED_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    @obs.observe(name="assistant.ask", capture_output=False)
    def run(question: str):
        # LangGraph 흐름은 동기 코드(모델·DB 호출) - 별도 스레드에서 돌리고 이벤트를 큐로 넘긴다
        t0 = time.perf_counter()
        obs.trace(name="assistant.ask", input=question, tags=["site"])
        first, outcome = None, "error"
        try:
            for ev in GRAPH.stream({"question": question}, stream_mode="custom"):
                kind = ev.pop("type")
                if kind == "token" and first is None:
                    first = time.perf_counter() - t0
                    metrics.FIRST_TOKEN.observe(first)
                elif kind == "plan":
                    metrics.INTENT.labels(ev.get("intent") or "-").inc()
                elif kind == "done":
                    outcome = metrics.observe_done(ev.get("checks") or {})
                loop.call_soon_threadsafe(queue.put_nowait, (kind, ev))
            if outcome == "error":                                # 매매 권유 거절·범위 밖은 done 없이 안내문만 나간다
                outcome = "declined"
        except Exception as e:                                    # 내부 오류 내용은 사용자에게 그대로 보이지 않게
            print("[ask] error:", repr(e), flush=True)
            obs.span(level="ERROR", status_message=repr(e))
            loop.call_soon_threadsafe(queue.put_nowait, ("error", {"message": "답변을 만들지 못했습니다. 잠시 후 다시 시도해 주세요."}))
        finally:
            metrics.ASK.labels(outcome).inc()
            metrics.ASK_SECONDS.observe(time.perf_counter() - t0)
            loop.call_soon_threadsafe(queue.put_nowait, ("_end", {"ms": round((time.perf_counter() - t0) * 1000)}))

    threading.Thread(target=run, args=(body.question.strip(),), daemon=True).start()

    async def events():
        while True:
            kind, data = await queue.get()
            if kind == "_end":
                yield sse("end", data)
                return
            yield sse(kind, data)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
