// POST /api/assistant  {question}  →  text/event-stream (AI 비서 오케스트레이터 중계)
// - 오케스트레이터 주소: env.ASSIST_URL (로컬 http://127.0.0.1:8100, 운영은 GPU 서버)
// - Worker ↔ 오케스트레이터 인증: env.ASSIST_SECRET (헤더 X-Assist-Secret)
// - 하루 질문 수 제한: 접속 IP(해시) 기준, D1(BOARD_DB)의 assist_usage 표에 집계
import { json, bad } from "./_utils.js";

export const DAILY_LIMIT = 20;

async function ipHash(request) {
  const ip = request.headers.get("CF-Connecting-IP") || "0.0.0.0";
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode("moastock-assist:" + ip));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 16);
}

const kstDay = () => new Date(Date.now() + 9 * 3600e3).toISOString().slice(0, 10);

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

export async function onRequestGet(context) {
  // 남은 횟수 조회 (화면 표시용)
  const { request, env } = context;
  if (!env.ASSIST_URL) return json({ available: false, limit: DAILY_LIMIT, remaining: 0 });
  let used = 0;
  if (env.BOARD_DB) {
    const row = await env.BOARD_DB.prepare("SELECT n FROM assist_usage WHERE ip_hash = ? AND day = ?")
      .bind(await ipHash(request), kstDay()).first().catch(() => null);
    used = row ? row.n : 0;
  }
  return json({ available: true, limit: DAILY_LIMIT, remaining: Math.max(0, DAILY_LIMIT - used) });
}

export async function onRequestPost(context) {
  const { request, env } = context;
  if (!env.ASSIST_URL) return json({ error: "AI 비서가 아직 준비 중입니다" }, { status: 503 });
  const body = await request.json().catch(() => null);
  const question = typeof body?.question === "string" ? body.question.trim() : "";
  if (question.length < 2 || question.length > 500) return bad("질문은 2~500자로 입력해 주세요");

  if (env.BOARD_DB) {
    const q = await takeQuota(env.BOARD_DB, await ipHash(request));
    if (!q.ok) return json({ error: `오늘 질문 한도(${DAILY_LIMIT}회)를 모두 썼습니다. 내일 다시 이용해 주세요` }, { status: 429 });
  }

  let upstream;
  try {
    upstream = await fetch(`${env.ASSIST_URL.replace(/\/$/, "")}/v1/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(env.ASSIST_SECRET ? { "X-Assist-Secret": env.ASSIST_SECRET } : {}) },
      body: JSON.stringify({ question }),
    });
  } catch (err) {
    return json({ error: "AI 비서 서버에 연결하지 못했습니다" }, { status: 502 });
  }
  if (!upstream.ok || !upstream.body) return json({ error: "AI 비서 서버 오류" }, { status: 502 });
  // 스트림을 그대로 흘려보낸다
  return new Response(upstream.body, {
    headers: { "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-store" },
  });
}
