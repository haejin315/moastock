// /api/chat/* : 실시간 채팅 (별도 Worker "moastock-chat"의 Durable Object로 연결)
//   GET  /api/chat/rooms            방 목록 + 제한값
//   POST /api/chat/rooms            방 만들기 {title, password?, capacity, client}
//   GET  /api/chat/rooms/:id/ws     WebSocket 입장 (비밀번호·닉네임은 연결 후 첫 메시지로)
// 로비의 내부 경로(상태 갱신·삭제)는 여기서 열지 않는다 - 방 DO만 부를 수 있다.
import { json, bad } from "./_utils.js";

const ROOM_ID = /^[a-z0-9]{6,16}$/;

async function ipHash(request) {
  const ip = request.headers.get("CF-Connecting-IP") || "0.0.0.0";
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode("moastock-chat:" + ip));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 16);
}

export async function onRequest(context) {
  const { request, env, params } = context;
  if (!env.CHAT_LOBBY || !env.CHAT_ROOM) return json({ error: "채팅 서버가 연결되지 않았습니다" }, { status: 503 });
  const parts = [].concat(params.path || []);
  const lobby = env.CHAT_LOBBY.get(env.CHAT_LOBBY.idFromName("lobby"));

  if (parts.length === 1 && parts[0] === "rooms") {
    if (request.method === "GET") return lobby.fetch("https://lobby/rooms");
    if (request.method === "POST") {
      const body = await request.json().catch(() => null);
      if (!body || typeof body !== "object") return bad("잘못된 요청");
      return lobby.fetch("https://lobby/rooms", {
        method: "POST",
        body: JSON.stringify({ title: body.title, password: body.password, capacity: body.capacity,
                               client: body.client, ip: await ipHash(request) }),
      });
    }
    return bad("허용되지 않은 메서드", 405);
  }

  if (parts.length === 3 && parts[0] === "rooms" && parts[2] === "ws") {
    if (!ROOM_ID.test(parts[1])) return bad("잘못된 방 주소");
    if (request.headers.get("Upgrade") !== "websocket") return bad("WebSocket 연결만 받습니다", 426);
    const room = env.CHAT_ROOM.get(env.CHAT_ROOM.idFromName(parts[1]));
    const headers = new Headers(request.headers);
    headers.set("X-Chat-Ip", await ipHash(request));
    return room.fetch(new Request("https://room/ws", { headers }));
  }

  return bad("없는 경로", 404);
}
