// 모아스톡 채팅 Worker: Durable Object 클래스만 내보낸다.
// 사이트(Pages Functions /api/chat/*)가 DO 바인딩으로 직접 부르므로 Worker 자체 경로는 쓰지 않는다.
export { Lobby } from "./lobby.js";
export { ChatRoom } from "./room.js";

export default {
  fetch() {
    return new Response("moastock chat", { status: 404 });
  },
};
