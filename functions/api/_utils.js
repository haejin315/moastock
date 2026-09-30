// 공통 유틸: JSON 응답 + Cloudflare 엣지 캐시 래퍼.
// 모든 /api/* 는 외부 데이터 소스의 프록시다 — 브라우저 CORS 제약을 피하고,
// 엣지 캐시로 원본 API 호출량과 지연을 줄인다.

export function json(data, { status = 200, maxAge = 0 } = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      ...(maxAge > 0
        ? { "Cache-Control": `public, max-age=${maxAge}` }
        : { "Cache-Control": "no-store" }),
    },
  });
}

export function bad(message, status = 400) {
  return json({ error: message }, { status });
}

// 같은 URL 요청을 maxAge초 동안 엣지에 캐시한다.
export async function cached(context, maxAge, produce) {
  const cache = caches.default;
  const key = new Request(context.request.url, { method: "GET" });
  const hit = await cache.match(key);
  if (hit) return hit;
  const resp = await produce();
  if (resp.status === 200 && maxAge > 0) {
    const toStore = resp.clone();
    context.waitUntil(cache.put(key, toStore));
  }
  return resp;
}

export async function fetchUpstream(url, accept = "application/json") {
  const resp = await fetch(url, {
    headers: {
      // 일부 원본(야후, 언론사 RSS)은 UA 없는 요청을 거부한다
      "User-Agent":
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) moastock/1.0 (+https://github.com/haejin315/moastock)",
      Accept: accept,
    },
    cf: { cacheTtl: 30 },
  });
  if (!resp.ok) throw new Error(`upstream ${resp.status}`);
  return resp;
}
