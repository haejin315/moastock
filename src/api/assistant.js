// POST /api/assistant  {question}  →  text/event-stream (AI 비서 오케스트레이터 중계)
// - 오케스트레이터 연결: 운영은 env.ASSIST_VPC (Workers VPC 바인딩 → Cloudflare 터널 → GPU 서버, 공개 주소 없음)
//                       로컬·임시는 env.ASSIST_URL (예: http://127.0.0.1:8100)
// - Worker ↔ 오케스트레이터 인증: env.ASSIST_SECRET (헤더 X-Assist-Secret)
// - 운영 시간: env.ASSIST_HOURS "15-22" (KST). GPU 서버가 이 시간에만 켜져 있어 시간 밖에는 서버를 부르지 않는다
// - 하루 질문 수 제한: 접속 IP(해시) 기준, D1(BOARD_DB)의 assist_usage 표에 집계. 서버 오류로 답을 못 하면 되돌린다
import { json, bad } from "./_utils.js";

export const DAILY_LIMIT = 20;

async function ipHash(request) {
  const ip = request.headers.get("CF-Connecting-IP") || "0.0.0.0";
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode("moastock-assist:" + ip));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 16);
}

/** 오케스트레이터로 보내는 fetch - VPC 바인딩이 있으면 그쪽(비공개 터널)으로 */
function upstream(env, path, init) {
  if (env.ASSIST_VPC) return env.ASSIST_VPC.fetch(`http://127.0.0.1:8100${path}`, init);
  return fetch(`${env.ASSIST_URL.replace(/\/$/, "")}${path}`, init);
}
const configured = (env) => Boolean(env.ASSIST_VPC || env.ASSIST_URL);

const kstNow = (now = Date.now()) => new Date(now + 9 * 3600e3);
const kstDay = () => kstNow().toISOString().slice(0, 10);

/** 운영 시간 "15-22" → {start:15, end:22}. 값이 없거나 잘못되면 null (= 항상 운영) */
export function parseHours(s) {
  const m = /^\s*(\d{1,2})\s*-\s*(\d{1,2})\s*$/.exec(s || "");
  if (!m) return null;
  const start = +m[1], end = +m[2];
  return start >= 0 && end <= 24 && start < end ? { start, end } : null;
}

export function isOpen(hours, now = Date.now()) {
  if (!hours) return true;
  const h = kstNow(now).getUTCHours();
  return h >= hours.start && h < hours.end;
}

const hoursText = (hours) => `${String(hours.start).padStart(2, "0")}:00~${String(hours.end).padStart(2, "0")}:00`;

/** 오늘 사용량을 1 늘리고 늘린 뒤 값을 돌려준다 (한도 초과면 늘리지 않음) */
async function takeQuota(db, key) {
  await db.prepare("CREATE TABLE IF NOT EXISTS assist_usage (ip_hash TEXT, day TEXT, n INTEGER, PRIMARY KEY (ip_hash, day))").run();
  const day = kstDay();
  const row = await db.prepare("SELECT n FROM assist_usage WHERE ip_hash = ? AND day = ?").bind(key, day).first();
  const used = row ? row.n : 0;
  if (used >= DAILY_LIMIT) return { ok: false, used };
  await db.prepare("INSERT INTO assist_usage (ip_hash, day, n) VALUES (?, ?, 1) " +
                   "ON CONFLICT(ip_hash, day) DO UPDATE SET n = n + 1").bind(key, day).run();
  return { ok: true, used: used + 1 };
}

async function refundQuota(db, key) {
  await db.prepare("UPDATE assist_usage SET n = n - 1 WHERE ip_hash = ? AND day = ? AND n > 0").bind(key, kstDay()).run();
}

export async function onRequestGet(context) {
  // 남은 횟수·운영 시간 조회 (화면 표시용)
  const { request, env } = context;
  if (!configured(env)) return json({ available: false, limit: DAILY_LIMIT, remaining: 0 });
  const hours = parseHours(env.ASSIST_HOURS);
  let used = 0;
  if (env.BOARD_DB) {
    const row = await env.BOARD_DB.prepare("SELECT n FROM assist_usage WHERE ip_hash = ? AND day = ?")
      .bind(await ipHash(request), kstDay()).first().catch(() => null);
    used = row ? row.n : 0;
  }
  return json({ available: true, open: isOpen(hours), hours: hours ? hoursText(hours) : null,
                limit: DAILY_LIMIT, remaining: Math.max(0, DAILY_LIMIT - used) });
}

export async function onRequestPost(context) {
  const { request, env } = context;
  if (!configured(env)) return json({ error: "AI 비서가 아직 준비 중입니다" }, { status: 503 });
  const hours = parseHours(env.ASSIST_HOURS);
  if (!isOpen(hours)) return json({ error: `AI 비서 운영 시간은 매일 ${hoursText(hours)}입니다` }, { status: 503 });
  const body = await request.json().catch(() => null);
  const question = typeof body?.question === "string" ? body.question.trim() : "";
  if (question.length < 2 || question.length > 500) return bad("질문은 2~500자로 입력해 주세요");

  const key = env.BOARD_DB ? await ipHash(request) : null;
  if (key) {
    const q = await takeQuota(env.BOARD_DB, key);
    if (!q.ok) return json({ error: `오늘 질문 한도(${DAILY_LIMIT}회)를 모두 썼습니다. 내일 다시 이용해 주세요` }, { status: 429 });
  }
  const fail = async (msg) => {
    if (key) await refundQuota(env.BOARD_DB, key).catch(() => {});
    return json({ error: msg }, { status: 502 });
  };

  let res;
  try {
    res = await upstream(env, "/v1/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(env.ASSIST_SECRET ? { "X-Assist-Secret": env.ASSIST_SECRET } : {}) },
      body: JSON.stringify({ question }),
    });
  } catch (err) {
    return fail("AI 비서 서버가 잠시 점검 중입니다. 조금 뒤 다시 시도해 주세요");
  }
  if (!res.ok || !res.body) return fail("AI 비서 서버가 잠시 점검 중입니다. 조금 뒤 다시 시도해 주세요");
  // 스트림을 그대로 흘려보낸다
  return new Response(res.body, {
    headers: { "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-store" },
  });
}
