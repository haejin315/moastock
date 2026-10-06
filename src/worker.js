// 모아스톡 Worker: 정적 페이지(public/) + /api/* + 실시간 채팅 Durable Object를 한 곳에서.
// 정적 파일은 Workers Static Assets가 직접 내보내고(wrangler.toml [assets]), /api/* 만 이 코드가 받는다.
import { handleApi } from "./router.js";

export { Lobby } from "./chat/lobby.js";
export { ChatRoom } from "./chat/room.js";

export default {
  async fetch(request, env, ctx) {
    const { pathname } = new URL(request.url);
    if (pathname === "/api" || pathname.startsWith("/api/")) return handleApi(request, env, ctx);
    return env.ASSETS.fetch(request);           // run_worker_first 밖의 경로는 보통 여기까지 오지 않는다
  },
};
