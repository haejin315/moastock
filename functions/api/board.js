// 종목 토론방: Cloudflare D1(서버리스 SQLite)에 저장되는 익명 게시판.
//   GET  /api/board?code=005930          최신 글 50개
//   POST /api/board {code, nick, body}   글 쓰기
// 인증 없는 공개 게시판이므로: 길이 제한, HTML 이스케이프(클라이언트), IP 해시
// 기반 분당 쓰기 제한, 삭제는 운영자(D1 콘솔)만. 바인딩(DB)이 없으면 안내만.
import { json, bad } from "./_utils.js";

const MAX_BODY = 500;
const MAX_NICK = 20;
const WRITES_PER_MINUTE = 3;

async function ensureSchema(db) {
  await db.exec(
    "CREATE TABLE IF NOT EXISTS posts (" +
    "id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, nick TEXT NOT NULL, " +
    "body TEXT NOT NULL, ip_hash TEXT NOT NULL, created_at TEXT NOT NULL); " +
    "CREATE INDEX IF NOT EXISTS idx_posts_code ON posts(code, id DESC);",
  );
}

async function ipHash(request) {
  const ip = request.headers.get("CF-Connecting-IP") || "0.0.0.0";
  const data = new TextEncoder().encode("moastock:" + ip);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(digest)].slice(0, 8).map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function onRequestGet(context) {
  const db = context.env.BOARD_DB;
  if (!db) return json({ items: [], error: "board_unavailable" });
  const code = (new URL(context.request.url).searchParams.get("code") || "").trim();
  if (!/^\d{6}$/.test(code)) return bad("code는 6자리 종목코드여야 합니다");
  await ensureSchema(db);
  const { results } = await db
    .prepare("SELECT id, nick, body, created_at FROM posts WHERE code=? ORDER BY id DESC LIMIT 50")
    .bind(code)
    .all();
  return json({ code, items: results || [] });
}

export async function onRequestPost(context) {
  const db = context.env.BOARD_DB;
  if (!db) return bad("토론방 저장소가 아직 설정되지 않았습니다", 503);
  let payload;
  try { payload = await context.request.json(); } catch (e) { return bad("JSON 본문이 필요합니다"); }
  const code = String(payload.code || "").trim();
  const nick = String(payload.nick || "").trim().slice(0, MAX_NICK) || "익명";
  const body = String(payload.body || "").trim();
  if (!/^\d{6}$/.test(code)) return bad("code는 6자리 종목코드여야 합니다");
  if (!body) return bad("내용을 입력하세요");
  if (body.length > MAX_BODY) return bad(`내용은 ${MAX_BODY}자 이내로 작성하세요`);

  await ensureSchema(db);
  const hash = await ipHash(context.request);
  const { results } = await db
    .prepare("SELECT COUNT(*) AS n FROM posts WHERE ip_hash=? AND created_at > datetime('now','-60 seconds')")
    .bind(hash)
    .all();
  if ((results?.[0]?.n || 0) >= WRITES_PER_MINUTE) {
    return bad("잠시 후 다시 작성해 주세요 (분당 작성 제한)", 429);
  }
  await db
    .prepare("INSERT INTO posts (code, nick, body, ip_hash, created_at) VALUES (?,?,?,?,datetime('now'))")
    .bind(code, nick, body, hash)
    .run();
  return json({ ok: true });
}
