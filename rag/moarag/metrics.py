"""단계별 처리량·소요시간 기록. 모든 단계가 metrics.jsonl 에 한 줄씩 남긴다.

    with Stage("crawl_news") as st:
        ...
        st.add(items_in=100, items_out=97, bytes=123456)
"""
from __future__ import annotations

import json
import time
from datetime import datetime

from .config import KST, METRICS_PATH


class Stage:
    def __init__(self, name: str, **params):
        self.name = name
        self.params = params
        self.counts: dict[str, float] = {}
        self.extra: dict = {}

    def add(self, **kv):
        for k, v in kv.items():
            self.counts[k] = self.counts.get(k, 0) + v

    def set(self, **kv):
        self.extra.update(kv)

    def __enter__(self):
        self.t0 = time.perf_counter()
        self.started = datetime.now(KST).isoformat(timespec="seconds")
        print(f"[{self.name}] 시작 {self.started} {self.params or ''}", flush=True)
        return self

    def elapsed(self) -> float:
        return time.perf_counter() - self.t0

    def __exit__(self, exc_type, exc, tb):
        sec = self.elapsed()
        rec = {
            "stage": self.name,
            "started": self.started,
            "finished": datetime.now(KST).isoformat(timespec="seconds"),
            "seconds": round(sec, 2),
            "status": "error" if exc else "ok",
            "params": self.params,
            "counts": self.counts,
            # 처리량: 카운트별 초당 처리 건수
            "per_sec": {k: round(v / sec, 2) for k, v in self.counts.items() if sec > 0},
            **({"error": repr(exc)} if exc else {}),
            **self.extra,
        }
        with METRICS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[{self.name}] {'완료' if not exc else '실패'} {sec:,.1f}s {self.counts}", flush=True)
        return False


class Progress:
    """긴 루프용 간이 진행 표시 (N초마다 한 줄)."""

    def __init__(self, label: str, total: int | None = None, every: float = 30.0):
        self.label, self.total, self.every = label, total, every
        self.n = 0
        self.t0 = self.last = time.perf_counter()

    def tick(self, k: int = 1, **info):
        self.n += k
        now = time.perf_counter()
        if now - self.last >= self.every:
            self.last = now
            rate = self.n / (now - self.t0)
            eta = f" ETA {(self.total - self.n) / rate / 60:,.1f}분" if self.total and rate else ""
            tot = f"/{self.total:,}" if self.total else ""
            extra = " ".join(f"{k}={v}" for k, v in info.items())
            print(f"  {self.label}: {self.n:,}{tot} ({rate:,.1f}/s){eta} {extra}", flush=True)
