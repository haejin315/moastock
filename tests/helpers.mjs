// Pages Functions 테스트용 가짜 런타임: fetch·엣지 캐시·context를 흉내 낸다.
// 실제 네트워크는 쓰지 않는다 - CI(Cloudflare 빌드)에서 외부 API 상태와 무관하게 결정적으로 돈다.

export function installFakeCache() {
  const store = new Map();
  globalThis.caches = {
    default: {
      match: async (req) => store.get(req.url)?.clone(),
      put: async (req, resp) => { store.set(req.url, resp); },
    },
  };
  return store;
}

/**
 * routes: [[RegExp|string, handler(url) => Response|object|Error]]
 * 일치하는 첫 경로의 응답을 돌려주고, 호출된 URL을 calls에 쌓는다.
 */
export function mockFetch(routes) {
  const calls = [];
  globalThis.fetch = async (input) => {
    const url = typeof input === "string" ? input : input.url;
    calls.push(url);
    for (const [pat, handler] of routes) {
      if (typeof pat === "string" ? url.includes(pat) : pat.test(url)) {
        const out = await handler(url);
        if (out instanceof Error) throw out;
        if (out instanceof Response) return out;
        return new Response(typeof out === "string" ? out : JSON.stringify(out), {
          status: 200,
          headers: { "Content-Type": typeof out === "string" ? "text/xml" : "application/json" },
        });
      }
    }
    throw new Error(`테스트에서 예상하지 못한 요청: ${url}`);
  };
  return calls;
}

export function ctx(path, { env = {}, method = "GET", body } = {}) {
  const waits = [];
  return {
    request: new Request(`https://moastock.test${path}`, {
      method,
      body: body ? JSON.stringify(body) : undefined,
      headers: body ? { "Content-Type": "application/json" } : undefined,
    }),
    env,
    waitUntil: (p) => waits.push(p),
    waits,
  };
}

export async function bodyOf(resp) {
  return { status: resp.status, data: await resp.json(), headers: resp.headers };
}
