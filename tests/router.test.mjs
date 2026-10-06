// /api/* 라우터 (src/router.js) - 경로·메서드 분기와 오류 처리
import test, { beforeEach } from "node:test";
import assert from "node:assert/strict";
import { handleApi } from "../src/router.js";
import { installFakeCache, mockFetch } from "./helpers.mjs";

const ctx = { waitUntil() {} };
const call = (path, init, env = {}) => handleApi(new Request(`https://moastock.test${path}`, init), env, ctx);

beforeEach(() => installFakeCache());

test("알려진 API로 분기 (GET)", async () => {
  mockFetch([["/v8/finance/chart/", () => ({ chart: { result: [{ meta: { symbol: "AAPL", regularMarketPrice: 2, chartPreviousClose: 1 },
    indicators: { quote: [{ close: [1, 2] }] } }] } })]]);
  const r = await call("/api/quote?symbols=AAPL");
  assert.equal(r.status, 200);
  assert.equal((await r.json()).quotes[0].symbol, "AAPL");
});

test("없는 API와 하위 경로는 404", async () => {
  for (const p of ["/api/nope", "/api/quote/extra", "/api"]) {
    assert.equal((await call(p)).status, 404, p);
  }
});

test("지원하지 않는 메서드는 405", async () => {
  assert.equal((await call("/api/quote?symbols=AAPL", { method: "POST" })).status, 405);
});

test("핸들러 예외는 500으로 감싸고 내부 메시지는 노출하지 않는다", async () => {
  const r = await call("/api/board?code=005930", {}, { BOARD_DB: { prepare() { throw new Error("db secret detail"); } } });
  assert.equal(r.status, 500);
  assert.ok(!(await r.text()).includes("secret detail"));
});

test("채팅 경로는 하위 경로를 받고, 바인딩이 없으면 503", async () => {
  assert.equal((await call("/api/chat/rooms")).status, 503);
});

test("채팅 방 목록은 로비 DO로 전달", async () => {
  let seen = null;
  const stub = { fetch: async (url) => { seen = String(url); return new Response('{"rooms":[]}'); } };
  const ns = { idFromName: (n) => n, get: () => stub };
  const r = await call("/api/chat/rooms", {}, { CHAT_LOBBY: ns, CHAT_ROOM: ns });
  assert.equal(r.status, 200);
  assert.equal(seen, "https://lobby/rooms");
});
