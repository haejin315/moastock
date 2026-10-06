// 채팅방 하나 = Durable Object 하나. WebSocket 휴면(hibernation) API를 써서
// 대화가 없을 때는 DO가 잠들어 무료 한도(실행 시간)를 거의 쓰지 않는다.
//
// 클라이언트 → 서버 메시지 (JSON)
//   {t:"join", nick, client, password?, owner?}   입장 (비밀번호·방장 토큰은 URL이 아니라 여기로)
//   {t:"msg", text}                                대화
//   {t:"update", owner, title?, password?, capacity?}  방장: 설정 변경 (password "" = 공개방으로)
//   {t:"delete", owner}                            방장: 방 삭제
// 서버 → 클라이언트
//   welcome / msg / sys / members / meta / error / closed
import { DurableObject } from "cloudflare:workers";
import { LIMITS, checkCapacity, checkMessage, checkNick, checkPassword, checkTitle, rateAllow, uniqueNick } from "./rules.js";
import { hashPassword, safeEqual, sha256 } from "./secure.js";

const HOUR = 3600 * 1000;

export class ChatRoom extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    this.sql.exec("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)");
    this.sql.exec(`CREATE TABLE IF NOT EXISTS messages (
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, nick TEXT NOT NULL, ch TEXT NOT NULL, text TEXT NOT NULL)`);
    // 연결 유지용 ping은 DO를 깨우지 않고 런타임이 바로 답한다
    ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair('{"t":"ping"}', '{"t":"pong"}'));
  }

  // ---- 저장소 도우미 ----------------------------------------------------------
  getMeta() {
    const rows = this.sql.exec("SELECT k, v FROM meta").toArray();
    if (!rows.length) return null;
    return Object.fromEntries(rows.map((r) => [r.k, JSON.parse(r.v)]));
  }
  setMeta(obj) {
    for (const [k, v] of Object.entries(obj)) {
      this.sql.exec("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", k, JSON.stringify(v));
    }
  }
  publicMeta(m = this.getMeta()) {
    return { id: m.id, title: m.title, capacity: m.capacity, locked: !!m.pwHash, permanent: !!m.permanent };
  }
  history() {
    return this.sql.exec(`SELECT id, ts, nick, ch, text FROM messages ORDER BY id DESC LIMIT ${LIMITS.history}`)
      .toArray().reverse();
  }
  joined() {
    return this.ctx.getWebSockets().filter((ws) => ws.deserializeAttachment()?.joined);
  }
  members() {
    return this.joined().map((ws) => ws.deserializeAttachment().nick);
  }
  send(ws, obj) {
    try { ws.send(JSON.stringify(obj)); } catch { /* 이미 닫힌 연결 */ }
  }
  broadcast(obj) {
    const data = JSON.stringify(obj);
    for (const ws of this.joined()) { try { ws.send(data); } catch { /* 무시 */ } }
  }

  async notifyLobby() {
    const m = this.getMeta();
    if (!m) return;
    const lobby = this.env.CHAT_LOBBY.get(this.env.CHAT_LOBBY.idFromName("lobby"));
    await lobby.fetch(`https://lobby/rooms/${m.id}/state`, {
      method: "POST",
      body: JSON.stringify({ title: m.title, locked: !!m.pwHash, capacity: m.capacity, members: this.joined().length }),
    }).catch(() => {});
  }

  // ---- HTTP: 초기화(로비 전용) / WebSocket 연결 ---------------------------------------
  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === "/init" && request.method === "POST") {
      const b = await request.json();
      const pw = b.password ? await hashPassword(b.password) : null;
      this.setMeta({ id: b.id, title: b.title, capacity: b.capacity, permanent: !!b.permanent,
                     ownerHash: b.ownerHash || null, pwHash: pw?.hash || null, pwSalt: pw?.salt || null,
                     lastActive: Date.now() });
      await this.ctx.storage.setAlarm(Date.now() + HOUR);
      return new Response("ok");
    }
    if (url.pathname === "/ws") {
      if (request.headers.get("Upgrade") !== "websocket") return new Response("WebSocket 전용", { status: 426 });
      if (!this.getMeta()) return new Response("없는 방입니다", { status: 404 });
      const pair = new WebSocketPair();
      const [client, server] = Object.values(pair);
      this.ctx.acceptWebSocket(server);
      server.serializeAttachment({ joined: false, ip: request.headers.get("X-Chat-Ip") || "" });
      return new Response(null, { status: 101, webSocket: client });
    }
    return new Response("not found", { status: 404 });
  }

  // ---- WebSocket 이벤트 (휴면에서 깨어나 호출됨) ---------------------------------------
  async webSocketMessage(ws, raw) {
    if (typeof raw !== "string" || raw.length > 4000) return this.send(ws, { t: "error", msg: "잘못된 메시지" });
    let m;
    try { m = JSON.parse(raw); } catch { return this.send(ws, { t: "error", msg: "잘못된 메시지" }); }
    const att = ws.deserializeAttachment() || {};
    const meta = this.getMeta();
    if (!meta) { this.send(ws, { t: "closed", msg: "없는 방입니다" }); return ws.close(4404, "gone"); }

    if (m.t === "join") return this.onJoin(ws, att, meta, m);
    if (!att.joined) return this.send(ws, { t: "error", msg: "먼저 입장하세요" });
    if (m.t === "msg") return this.onChat(ws, att, m);
    if (m.t === "update") return this.onUpdate(ws, meta, m);
    if (m.t === "delete") return this.onDelete(ws, meta, m);
    return this.send(ws, { t: "error", msg: "알 수 없는 요청" });
  }

  async onJoin(ws, att, meta, m) {
    if (att.joined) return;
    const nick = checkNick(m.nick);
    if (nick.error) { this.send(ws, { t: "error", code: "nick", msg: nick.error }); return ws.close(4400, "nick"); }
    if (typeof m.client !== "string" || m.client.length < 8 || m.client.length > 64) return ws.close(4400, "client");
    if (meta.pwHash) {
      const { hash } = await hashPassword(String(m.password || ""), meta.pwSalt);
      if (!safeEqual(hash, meta.pwHash)) {
        this.send(ws, { t: "error", code: "password", msg: "비밀번호가 맞지 않습니다" });
        return ws.close(4403, "password");
      }
    }
    if (this.joined().length >= meta.capacity) {
      this.send(ws, { t: "error", code: "full", msg: `정원(${meta.capacity}명)이 찼습니다` });
      return ws.close(4429, "full");
    }
    const owner = !!(meta.ownerHash && m.owner && safeEqual(await sha256(String(m.owner)), meta.ownerHash));
    const name = uniqueNick(nick.value, this.members());
    const ch = (await sha256(`moastock-chat:${m.client}`)).slice(0, 12);   // 내 메시지 표시용 (ID 원문은 노출 안 함)
    ws.serializeAttachment({ ...att, joined: true, nick: name, ch, owner, times: [] });
    this.send(ws, { t: "welcome", room: this.publicMeta(meta), me: { nick: name, ch, owner },
                    members: this.members(), history: this.history(), limits: { messageMax: LIMITS.messageMax } });
    this.broadcast({ t: "sys", text: `${name}님이 들어왔습니다`, members: this.members() });
    this.setMeta({ lastActive: Date.now() });
    await this.notifyLobby();
  }

  async onChat(ws, att, m) {
    const rate = rateAllow(att.times, Date.now());
    ws.serializeAttachment({ ...att, times: rate.times });
    if (!rate.ok) return this.send(ws, { t: "error", code: "rate", msg: "메시지를 너무 빨리 보내고 있습니다. 잠시 후 다시 보내세요" });
    const text = checkMessage(m.text);
    if (text.error) return this.send(ws, { t: "error", code: "msg", msg: text.error });
    const ts = Date.now();
    const id = this.sql.exec("INSERT INTO messages (ts, nick, ch, text) VALUES (?,?,?,?) RETURNING id",
      ts, att.nick, att.ch, text.value).one().id;
    this.sql.exec(`DELETE FROM messages WHERE id <= ?`, id - LIMITS.history);   // 최근 N개만 보관
    this.broadcast({ t: "msg", id, ts, nick: att.nick, ch: att.ch, text: text.value });
    this.setMeta({ lastActive: ts });
  }

  async isOwner(meta, token) {
    return !!(meta.ownerHash && token && safeEqual(await sha256(String(token)), meta.ownerHash));
  }

  async onUpdate(ws, meta, m) {
    if (!(await this.isOwner(meta, m.owner))) return this.send(ws, { t: "error", msg: "방장만 바꿀 수 있습니다" });
    const patch = {};
    if (m.title !== undefined) {
      const t = checkTitle(m.title);
      if (t.error) return this.send(ws, { t: "error", msg: t.error });
      patch.title = t.value;
    }
    if (m.capacity !== undefined) {
      const c = checkCapacity(m.capacity);
      if (c.error) return this.send(ws, { t: "error", msg: c.error });
      if (c.value < this.joined().length) return this.send(ws, { t: "error", msg: "현재 인원보다 적게 줄일 수 없습니다" });
      patch.capacity = c.value;
    }
    if (m.password !== undefined) {
      const p = checkPassword(m.password);
      if (p.error) return this.send(ws, { t: "error", msg: p.error });
      if (p.value) { const h = await hashPassword(p.value); patch.pwHash = h.hash; patch.pwSalt = h.salt; }
      else { patch.pwHash = null; patch.pwSalt = null; }
    }
    this.setMeta(patch);
    const now = this.getMeta();
    this.broadcast({ t: "meta", room: this.publicMeta(now) });
    this.broadcast({ t: "sys", text: "방장이 방 설정을 바꿨습니다", members: this.members() });
    await this.notifyLobby();
  }

  async onDelete(ws, meta, m) {
    if (meta.permanent) return this.send(ws, { t: "error", msg: "기본 방은 삭제할 수 없습니다" });
    if (!(await this.isOwner(meta, m.owner))) return this.send(ws, { t: "error", msg: "방장만 삭제할 수 있습니다" });
    await this.destroy("방장이 방을 삭제했습니다");
  }

  async destroy(reason) {
    const meta = this.getMeta();
    for (const ws of this.ctx.getWebSockets()) {
      this.send(ws, { t: "closed", msg: reason });
      try { ws.close(4410, "deleted"); } catch { /* 무시 */ }
    }
    if (meta) {
      const lobby = this.env.CHAT_LOBBY.get(this.env.CHAT_LOBBY.idFromName("lobby"));
      await lobby.fetch(`https://lobby/rooms/${meta.id}`, { method: "DELETE" }).catch(() => {});
    }
    await this.ctx.storage.deleteAlarm();
    await this.ctx.storage.deleteAll();
  }

  async webSocketClose(ws) {
    const att = ws.deserializeAttachment() || {};
    try { ws.close(); } catch { /* 이미 닫힘 */ }
    if (att.joined) {
      ws.serializeAttachment({ ...att, joined: false });
      this.broadcast({ t: "sys", text: `${att.nick}님이 나갔습니다`, members: this.members() });
      this.setMeta({ lastActive: Date.now() });
      await this.notifyLobby();
    }
  }

  async webSocketError(ws) {
    await this.webSocketClose(ws);
  }

  // 빈 방 정리: 1시간마다 확인, 6시간 동안 아무도 없으면 삭제 (기본 방 제외)
  async alarm() {
    const meta = this.getMeta();
    if (!meta) return;
    const empty = this.joined().length === 0;
    if (!meta.permanent && empty && Date.now() - (meta.lastActive || 0) > LIMITS.idleDeleteMs) {
      return this.destroy("오래 비어 있어 방이 정리되었습니다");
    }
    await this.ctx.storage.setAlarm(Date.now() + HOUR);
  }
}
