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

from graph import GRAPH

app = FastAPI(title="moastock assistant")
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

    def run():
        # LangGraph 흐름은 동기 코드(모델·DB 호출) - 별도 스레드에서 돌리고 이벤트를 큐로 넘긴다
        t0 = time.perf_counter()
        try:
            for ev in GRAPH.stream({"question": body.question.strip()}, stream_mode="custom"):
                loop.call_soon_threadsafe(queue.put_nowait, (ev.pop("type"), ev))
        except Exception as e:                                    # 내부 오류 내용은 사용자에게 그대로 보이지 않게
            print("[ask] error:", repr(e), flush=True)
            loop.call_soon_threadsafe(queue.put_nowait, ("error", {"message": "답변을 만들지 못했습니다. 잠시 후 다시 시도해 주세요."}))
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, ("_end", {"ms": round((time.perf_counter() - t0) * 1000)}))

    threading.Thread(target=run, daemon=True).start()

    async def events():
        while True:
            kind, data = await queue.get()
            if kind == "_end":
                yield sse("end", data)
                return
            yield sse(kind, data)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
