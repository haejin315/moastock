"""비동기 HTTP: 동시성 제한 + 초당 요청 상한 + 지수 백오프 재시도.

원천 사이트에 부담을 주지 않도록 RPS 상한을 둔다. 429/5xx/네트워크 오류는 재시도하고,
연속 실패가 쌓이면 전체 속도를 잠시 늦춘다.
"""
from __future__ import annotations

import asyncio
import random
import time

import aiohttp

from .config import USER_AGENT


class RateLimiter:
    def __init__(self, rps: float):
        self.interval = 1.0 / rps
        self.next_at = 0.0
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            now = time.monotonic()
            delay = self.next_at - now
            self.next_at = max(now, self.next_at) + self.interval
        if delay > 0:
            await asyncio.sleep(delay)

    def slow_down(self, sec: float):
        self.next_at = max(self.next_at, time.monotonic()) + sec


class Fetcher:
    def __init__(self, rps: float = 10, concurrency: int = 16, timeout: float = 20, retries: int = 4):
        self.limiter = RateLimiter(rps)
        self.sem = asyncio.Semaphore(concurrency)
        self.retries = retries
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.session: aiohttp.ClientSession | None = None
        self.stats = {"requests": 0, "retries": 0, "errors": 0, "bytes": 0}

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            timeout=self.timeout,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"},
            connector=aiohttp.TCPConnector(limit=64, ttl_dns_cache=300),
        )
        return self

    async def __aexit__(self, *a):
        await self.session.close()

    async def get(self, url: str, *, params=None, as_json=False, raw=False):
        """(status, final_url, body) 반환. 재시도 후에도 실패하면 예외."""
        last = None
        for attempt in range(self.retries + 1):
            await self.limiter.wait()
            try:
                async with self.sem:
                    self.stats["requests"] += 1
                    async with self.session.get(url, params=params, allow_redirects=True) as r:
                        if r.status == 429 or r.status >= 500:
                            raise aiohttp.ClientResponseError(
                                r.request_info, r.history, status=r.status, message="retryable")
                        data = await r.read()
                        self.stats["bytes"] += len(data)
                        if raw:
                            return r.status, str(r.url), data
                        if as_json:
                            import json
                            return r.status, str(r.url), json.loads(data.decode("utf-8")) if r.status == 200 else None
                        return r.status, str(r.url), data.decode(r.charset or "utf-8", errors="replace")
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                last = e
                if attempt == self.retries:
                    break
                self.stats["retries"] += 1
                backoff = min(60, 2 ** attempt) + random.random()
                if isinstance(e, aiohttp.ClientResponseError) and e.status == 429:
                    self.limiter.slow_down(backoff)     # 전체 속도를 늦춘다
                await asyncio.sleep(backoff)
        self.stats["errors"] += 1
        raise RuntimeError(f"GET 실패 {url}: {last!r}")
