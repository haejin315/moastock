// /api/assistant 중계: 준비 안 됨(503), 입력 검사, 하루 한도, 스트림 그대로 전달
import test, { beforeEach } from "node:test";
import assert from "node:assert/strict";
import * as assistant from "../src/api/assistant.js";
import { mockFetch } from "./helpers.mjs";

// 아주 작은 가짜 D1: assist_usage 표만 흉내
function fakeD1() {
  const rows = new Map();
  return {
    prepare(sql) {
      let args = [];
      const stmt = {
        bind(...a) { args = a; return stmt; },
        async run() {
          if (/^INSERT INTO assist_usage/.test(sql)) {
            const k = args.join("|"); rows.set(k, (rows.get(k) || 0) + 1);
          }
          return {};
        },
        async first() { const n = rows.get(args.join("|")); return n ? { n } : null; },
      };
      return stmt;
    },
  };
}
const post = (body, env) => assistant.onRequestPost({
  request: new Request("https://moastock.test/api/assistant", { method: "POST", body: JSON.stringify(body),
    headers: { "Content-Type": "application/json", "CF-Connecting-IP": "1.2.3.4" } }), env });

let calls;
beforeEach(() => {
  calls = mockFetch([["/v1/ask", () => new Response("event: token\ndata: {\"text\":\"안녕\"}\n\n",
    { headers: { "Content-Type": "text/event-stream" } })]]);
});

test("오케스트레이터 주소가 없으면 503 (준비 중)", async () => {
  assert.equal((await post({ question: "삼성전자 공시" }, {})).status, 503);
});

test("질문 길이 검사", async () => {
  const env = { ASSIST_URL: "http://orch", BOARD_DB: fakeD1() };
  assert.equal((await post({ question: "a" }, env)).status, 400);
  assert.equal((await post({ question: "가".repeat(501) }, env)).status, 400);
  assert.equal(calls.length, 0, "검사에 걸리면 오케스트레이터를 부르지 않는다");
});

test("스트림을 그대로 넘기고 공유 비밀을 헤더로 붙인다", async () => {
  let seenHeader = null;
  globalThis.fetch = async (url, init) => { seenHeader = init.headers["X-Assist-Secret"];
    return new Response("event: token\ndata: {}\n\n", { headers: { "Content-Type": "text/event-stream" } }); };
  const r = await post({ question: "삼성전자 공시" }, { ASSIST_URL: "http://orch/", ASSIST_SECRET: "s3", BOARD_DB: fakeD1() });
  assert.equal(r.status, 200);
  assert.match(r.headers.get("Content-Type"), /event-stream/);
  assert.match(await r.text(), /event: token/);
  assert.equal(seenHeader, "s3");
});

test(`하루 ${assistant.DAILY_LIMIT}회를 넘으면 429`, async () => {
  const env = { ASSIST_URL: "http://orch", BOARD_DB: fakeD1() };
  for (let i = 0; i < assistant.DAILY_LIMIT; i++) assert.equal((await post({ question: "삼성전자 공시" }, env)).status, 200);
  const r = await post({ question: "삼성전자 공시" }, env);
  assert.equal(r.status, 429);
  assert.match((await r.json()).error, /한도/);
});

test("오케스트레이터 연결 실패는 502", async () => {
  globalThis.fetch = async () => { throw new Error("ECONNREFUSED"); };
  assert.equal((await post({ question: "삼성전자 공시" }, { ASSIST_URL: "http://orch", BOARD_DB: fakeD1() })).status, 502);
});
