// 로비: 방 목록과 방 만들기 제한을 관리하는 Durable Object (인스턴스 하나, 이름 "lobby")
// 각 방(ChatRoom)은 인원·제목·잠금 상태가 바뀔 때 로비에 알려 목록을 최신으로 유지한다.
import { DurableObject } from "cloudflare:workers";
import { LIMITS, checkCapacity, checkPassword, checkTitle, isRoomId } from "./rules.js";
import { randomId, randomToken, sha256 } from "./secure.js";

const DEFAULT_ROOM = { id: "lobbyall01", title: "전체 채팅", capacity: LIMITS.maxCapacity };

const json = (data, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json; charset=utf-8" } });

export class Lobby extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    ctx.blockConcurrencyWhile(async () => {
      this.sql.exec(`CREATE TABLE IF NOT EXISTS rooms (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, locked INTEGER NOT NULL DEFAULT 0,
        capacity INTEGER NOT NULL, members INTEGER NOT NULL DEFAULT 0,
        creator TEXT, creator_ip TEXT, permanent INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL, last_active INTEGER NOT NULL)`);
      // 기본 방: 항상 있는 공개방
      if (!this.sql.exec("SELECT 1 FROM rooms WHERE id = ?", DEFAULT_ROOM.id).toArray().length) {
        await this.roomStub(DEFAULT_ROOM.id).fetch("https://room/init", {
          method: "POST",
          body: JSON.stringify({ ...DEFAULT_ROOM, password: null, ownerHash: null, permanent: true }),
        });
        const now = Date.now();
        this.sql.exec("INSERT INTO rooms (id, title, capacity, permanent, created_at, last_active) VALUES (?,?,?,1,?,?)",
          DEFAULT_ROOM.id, DEFAULT_ROOM.title, DEFAULT_ROOM.capacity, now, now);
      }
    });
  }

  roomStub(id) {
    return this.env.CHAT_ROOM.get(this.env.CHAT_ROOM.idFromName(id));
  }

  listRooms() {
    return this.sql.exec(
      "SELECT id, title, locked, capacity, members, permanent FROM rooms ORDER BY permanent DESC, members DESC, last_active DESC",
    ).toArray().map((r) => ({ ...r, locked: !!r.locked, permanent: !!r.permanent }));
  }

  async fetch(request) {
    const url = new URL(request.url);
    const parts = url.pathname.split("/").filter(Boolean);   // ["rooms", id?, action?]

    if (parts[0] !== "rooms") return json({ error: "not found" }, 404);

    // 방 목록
    if (parts.length === 1 && request.method === "GET") {
      return json({ rooms: this.listRooms(), limits: {
        maxRooms: LIMITS.maxRooms, maxCapacity: LIMITS.maxCapacity, minCapacity: LIMITS.minCapacity,
        defaultCapacity: LIMITS.defaultCapacity, maxRoomsPerCreator: LIMITS.maxRoomsPerCreator,
        titleMax: LIMITS.titleMax, messageMax: LIMITS.messageMax,
        passwordMin: LIMITS.passwordMin, passwordMax: LIMITS.passwordMax,
        nickMin: LIMITS.nickMin, nickMax: LIMITS.nickMax } });
    }

    // 방 만들기
    if (parts.length === 1 && request.method === "POST") {
      const body = await request.json().catch(() => ({}));
      const title = checkTitle(body.title);
      const cap = checkCapacity(body.capacity);
      const pw = checkPassword(body.password);
      const bad = title.error || cap.error || pw.error;
      if (bad) return json({ error: bad }, 400);
      if (typeof body.client !== "string" || body.client.length < 8 || body.client.length > 64) {
        return json({ error: "잘못된 요청" }, 400);
      }
      const total = this.sql.exec("SELECT COUNT(*) AS n FROM rooms").one().n;
      if (total >= LIMITS.maxRooms) {
        return json({ error: `방은 최대 ${LIMITS.maxRooms}개까지 열 수 있습니다. 빈 방이 정리되면 다시 시도해 주세요` }, 429);
      }
      // 같은 브라우저 ID 또는 같은 IP(해시)가 만든 방 수 - 브라우저 ID만 바꿔 우회하지 못하게
      const creatorKey = await sha256(body.client);
      const ipKey = String(body.ip || "").slice(0, 32) || "-";
      const mine = this.sql.exec(
        "SELECT COUNT(*) AS n FROM rooms WHERE creator = ? OR creator_ip = ?", creatorKey, ipKey,
      ).one().n;
      if (mine >= LIMITS.maxRoomsPerCreator) {
        return json({ error: `한 사람이 만들 수 있는 방은 ${LIMITS.maxRoomsPerCreator}개까지입니다` }, 429);
      }

      const id = randomId();
      const ownerToken = randomToken();
      const init = await this.roomStub(id).fetch("https://room/init", {
        method: "POST",
        body: JSON.stringify({ id, title: title.value, capacity: cap.value, password: pw.value,
                               ownerHash: await sha256(ownerToken), permanent: false }),
      });
      if (!init.ok) return json({ error: "방을 만들지 못했습니다" }, 500);
      const now = Date.now();
      this.sql.exec(
        "INSERT INTO rooms (id, title, locked, capacity, creator, creator_ip, created_at, last_active) VALUES (?,?,?,?,?,?,?,?)",
        id, title.value, pw.value ? 1 : 0, cap.value, creatorKey, ipKey, now, now);
      return json({ room: { id, title: title.value, locked: !!pw.value, capacity: cap.value, members: 0 }, ownerToken });
    }

    // 방이 보내는 상태 갱신 / 삭제 (내부 전용 - 사이트 프록시는 이 경로를 열지 않는다)
    const id = parts[1];
    if (!isRoomId(id)) return json({ error: "잘못된 방" }, 400);
    if (parts[2] === "state" && request.method === "POST") {
      const s = await request.json();
      this.sql.exec("UPDATE rooms SET title = ?, locked = ?, capacity = ?, members = ?, last_active = ? WHERE id = ?",
        s.title, s.locked ? 1 : 0, s.capacity, s.members, Date.now(), id);
      return json({ ok: true });
    }
    if (parts.length === 2 && request.method === "DELETE") {
      this.sql.exec("DELETE FROM rooms WHERE id = ? AND permanent = 0", id);
      return json({ ok: true });
    }
    return json({ error: "not found" }, 404);
  }
}
