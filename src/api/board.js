// 종목 토론방: Cloudflare D1(서버리스 SQLite)에 저장되는 익명 게시판.
//   GET    /api/board?code=005930                    최신 글 50개
//   POST   /api/board {code, nick, body, password}   글 쓰기
//   DELETE /api/board {id, password}                 작성 시 비밀번호로 삭제
// 인증 없는 공개 게시판이므로: 길이 제한, HTML 이스케이프(클라이언트), IP 해시
// 기반 분당 쓰기 제한, 삭제 실패 횟수 제한. 비밀번호는 PBKDF2로 해시해 저장한다.
import { json, bad } from "./_utils.js";

const MAX_BODY = 500;
const MAX_NICK = 20;
const WRITES_PER_MINUTE = 3;
const PW_MIN = 4;
const PW_MAX = 12;
const DELETE_FAILS_PER_10MIN = 5;
const PBKDF2_ITERATIONS = 20000;

let schemaReady = false;

async function ensureSchema(db) {
  if (schemaReady) return;
  await db.exec(
    "CREATE TABLE IF NOT EXISTS posts (" +
    "id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, nick TEXT NOT NULL, " +
    "body TEXT NOT NULL, ip_hash TEXT NOT NULL, created_at TEXT NOT NULL, " +
    "pw_hash TEXT, pw_salt TEXT);",
  );
  await db.exec("CREATE INDEX IF NOT EXISTS idx_posts_code ON posts(code, id DESC);");
  await db.exec(
    "CREATE TABLE IF NOT EXISTS delete_fails (ip_hash TEXT NOT NULL, created_at TEXT NOT NULL);",
  );
  // 비밀번호 컬럼이 생기기 전에 만들어진 테이블 보정 (이미 있으면 오류 - 무시)
  for (const col of ["pw_hash", "pw_salt"]) {
    try { await db.exec(`ALTER TABLE posts ADD COLUMN ${col} TEXT;`); } catch (e) { /* exists */ }
  }
  schemaReady = true;
}

const toHex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");

async function ipHash(request) {
  const ip = request.headers.get("CF-Connecting-IP") || "0.0.0.0";
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode("moastock:" + ip));
  return toHex(digest).slice(0, 16);
}

async function hashPassword(password, saltHex) {
  const salt = saltHex
    ? new Uint8Array(saltHex.match(/../g).map((h) => parseInt(h, 16)))
    : crypto.getRandomValues(new Uint8Array(16));
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveBits"],
  );
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt, iterations: PBKDF2_ITERATIONS, hash: "SHA-256" }, key, 256,
  );
  return { hash: toHex(bits), salt: toHex(salt) };
}

function validPassword(pw) {
  return typeof pw === "string" && pw.length >= PW_MIN && pw.length <= PW_MAX;
}

// 국내 6자리 종목코드, 또는 해외·코인 기호(AAPL, BRK-B, 7203.T, BTC-KRW)
const BOARD_CODE = /^(\d{6}|[A-Z0-9][A-Z0-9.-]{0,14})$/;

export async function onRequestGet(context) {
  const db = context.env.BOARD_DB;
  if (!db) return json({ items: [], error: "board_unavailable" });
  const code = (new URL(context.request.url).searchParams.get("code") || "").trim();
  if (!BOARD_CODE.test(code)) return bad("code는 6자리 종목코드 또는 해외·코인 기호여야 합니다");
  await ensureSchema(db);
  const { results } = await db
    .prepare(
      "SELECT id, nick, body, created_at, (pw_hash IS NOT NULL) AS deletable " +
      "FROM posts WHERE code=? ORDER BY id DESC LIMIT 50",
    )
    .bind(code)
    .all();
  return json({
    code,
    limits: { writesPerMinute: WRITES_PER_MINUTE, maxBody: MAX_BODY, pwMin: PW_MIN, pwMax: PW_MAX },
    items: results || [],
  });
}

export async function onRequestPost(context) {
  const db = context.env.BOARD_DB;
  if (!db) return bad("토론방 저장소가 아직 설정되지 않았습니다", 503);
  let payload;
  try { payload = await context.request.json(); } catch (e) { return bad("JSON 본문이 필요합니다"); }
  const code = String(payload.code || "").trim();
  const nick = String(payload.nick || "").trim().slice(0, MAX_NICK) || "익명";
  const body = String(payload.body || "").trim();
  const password = String(payload.password || "");
  if (!BOARD_CODE.test(code)) return bad("code는 6자리 종목코드 또는 해외·코인 기호여야 합니다");
  if (!body) return bad("내용을 입력하세요");
  if (body.length > MAX_BODY) return bad(`내용은 ${MAX_BODY}자 이내로 작성하세요`);
  if (!validPassword(password)) return bad(`비밀번호는 ${PW_MIN}~${PW_MAX}자로 입력하세요`);

  await ensureSchema(db);
  const hash = await ipHash(context.request);
  const { results } = await db
    .prepare("SELECT COUNT(*) AS n FROM posts WHERE ip_hash=? AND created_at > datetime('now','-60 seconds')")
    .bind(hash)
    .all();
  if ((results?.[0]?.n || 0) >= WRITES_PER_MINUTE) {
    return bad(`잠시 후 다시 작성해 주세요 (1인당 분당 ${WRITES_PER_MINUTE}회 작성 제한)`, 429);
  }
  const pw = await hashPassword(password);
  await db
    .prepare(
      "INSERT INTO posts (code, nick, body, ip_hash, created_at, pw_hash, pw_salt) " +
      "VALUES (?,?,?,?,datetime('now'),?,?)",
    )
    .bind(code, nick, body, hash, pw.hash, pw.salt)
    .run();
  return json({ ok: true });
}

export async function onRequestDelete(context) {
  const db = context.env.BOARD_DB;
  if (!db) return bad("토론방 저장소가 아직 설정되지 않았습니다", 503);
  let payload;
  try { payload = await context.request.json(); } catch (e) { return bad("JSON 본문이 필요합니다"); }
  const id = parseInt(payload.id, 10);
  const password = String(payload.password || "");
  if (!id) return bad("삭제할 글 id가 필요합니다");
  if (!validPassword(password)) return bad(`비밀번호는 ${PW_MIN}~${PW_MAX}자입니다`);

  await ensureSchema(db);
  const ip = await ipHash(context.request);
  const fails = await db
    .prepare("SELECT COUNT(*) AS n FROM delete_fails WHERE ip_hash=? AND created_at > datetime('now','-10 minutes')")
    .bind(ip)
    .all();
  if ((fails.results?.[0]?.n || 0) >= DELETE_FAILS_PER_10MIN) {
    return bad(`비밀번호를 ${DELETE_FAILS_PER_10MIN}회 틀려 10분간 삭제가 제한됩니다`, 429);
  }

  const row = await db.prepare("SELECT pw_hash, pw_salt FROM posts WHERE id=?").bind(id).first();
  if (!row) return bad("이미 삭제되었거나 없는 글입니다", 404);
  if (!row.pw_hash) return bad("비밀번호 없이 작성된 글은 삭제할 수 없습니다", 403);

  const check = await hashPassword(password, row.pw_salt);
  if (check.hash !== row.pw_hash) {
    await db.prepare("INSERT INTO delete_fails (ip_hash, created_at) VALUES (?, datetime('now'))").bind(ip).run();
    return bad("비밀번호가 일치하지 않습니다", 403);
  }
  await db.prepare("DELETE FROM posts WHERE id=?").bind(id).run();
  return json({ ok: true });
}
