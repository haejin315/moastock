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
          if (/^UPDATE assist_usage SET n = n - 1/.test(sql)) {
            const k = args.join("|"); if (rows.get(k) > 0) rows.set(k, rows.get(k) - 1);
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

test("VPC 바인딩이 있으면 공개 fetch 대신 바인딩(비공개 터널)으로 보낸다", async () => {
  globalThis.fetch = async () => { throw new Error("공개 fetch를 쓰면 안 된다"); };
  let seen = null;
  const ASSIST_VPC = { fetch: async (url, init) => { seen = { url, secret: init.headers["X-Assist-Secret"] };
    return new Response("event: token\ndata: {}\n\n", { headers: { "Content-Type": "text/event-stream" } }); } };
  const r = await post({ question: "삼성전자 공시" }, { ASSIST_VPC, ASSIST_SECRET: "s3", BOARD_DB: fakeD1() });
  assert.equal(r.status, 200);
  assert.match(seen.url, /\/v1\/ask$/);
  assert.equal(seen.secret, "s3");
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

test("운영 시간 해석과 판정 (KST)", () => {
  assert.deepEqual(assistant.parseHours("15-22"), { start: 15, end: 22 });
  assert.equal(assistant.parseHours(""), null);
  assert.equal(assistant.parseHours("22-15"), null);
  const h = { start: 15, end: 22 };
  const kst = (hh, mm = 0) => Date.UTC(2026, 9, 7, hh - 9, mm);   // KST hh:mm 을 UTC 시각으로
  assert.equal(assistant.isOpen(h, kst(14, 59)), false);
  assert.equal(assistant.isOpen(h, kst(15, 0)), true);
  assert.equal(assistant.isOpen(h, kst(21, 59)), true);
  assert.equal(assistant.isOpen(h, kst(22, 0)), false);
  assert.equal(assistant.isOpen(null, kst(3)), true);
});

test("운영 시간 밖에는 서버를 부르지 않고 503 + 시간 안내", async () => {
  const closed = Array.from({ length: 24 }, (_, i) => i).filter((i) => !assistant.isOpen({ start: i, end: i + 1 }))[0];
  const env = { ASSIST_URL: "http://orch", ASSIST_HOURS: `${closed}-${closed + 1}`, BOARD_DB: fakeD1() };
  const r = await post({ question: "삼성전자 공시" }, env);
  assert.equal(r.status, 503);
  assert.match((await r.json()).error, /운영 시간/);
  assert.equal(calls.length, 0);
  const g = await (await assistant.onRequestGet({ request: new Request("https://moastock.test/api/assistant"), env })).json();
  assert.equal(g.open, false);
  assert.match(g.hours, /^\d\d:00~\d\d:00$/);
});

test("서버 오류로 답을 못 하면 질문 횟수를 되돌린다", async () => {
  globalThis.fetch = async () => { throw new Error("ECONNREFUSED"); };
  const env = { ASSIST_URL: "http://orch", BOARD_DB: fakeD1() };
  for (let i = 0; i < 3; i++) assert.equal((await post({ question: "삼성전자 공시" }, env)).status, 502);
  const g = await (await assistant.onRequestGet({ request: new Request("https://moastock.test/api/assistant",
    { headers: { "CF-Connecting-IP": "1.2.3.4" } }), env })).json();
  assert.equal(g.remaining, assistant.DAILY_LIMIT);
});
