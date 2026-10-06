// /api/* 라우터 (Durable Object 클래스와 분리 - Node 테스트에서 바로 불러올 수 있게)
// API 모듈은 Pages Functions 시절과 같은 모양(onRequestGet 등, context = {request, env, params, waitUntil})을
// 유지해 그대로 재사용한다.
import * as board from "./api/board.js";
import * as chart from "./api/chart.js";
import * as chat from "./api/chat.js";
import * as dart from "./api/dart.js";
import * as news from "./api/news.js";
import * as quote from "./api/quote.js";
import * as screener from "./api/screener.js";
import * as stockfeed from "./api/stockfeed.js";
import { json } from "./api/_utils.js";

// /api/<이름> → 모듈 (chat 만 하위 경로를 받는다)
const API = { board, chart, dart, news, quote, screener, stockfeed };

function handlerFor(mod, method) {
  const m = method === "HEAD" ? "GET" : method;
  return mod[`onRequest${m[0]}${m.slice(1).toLowerCase()}`] || mod.onRequest;
}

export async function handleApi(request, env, ctx) {
  const url = new URL(request.url);
  const [, name, ...rest] = url.pathname.split("/").filter(Boolean);   // ["api", name, ...]
  const context = {
    request, env,
    params: { path: rest },
    waitUntil: (p) => ctx.waitUntil(p),
  };
  const mod = name === "chat" ? chat : API[name];
  if (!mod || (name !== "chat" && rest.length)) return json({ error: "없는 API입니다" }, { status: 404 });
  const fn = handlerFor(mod, request.method);
  if (!fn) return json({ error: "허용되지 않은 메서드" }, { status: 405 });
  try {
    return await fn(context);
  } catch (err) {
    console.error(`[api/${name}]`, err);
    return json({ error: "서버 오류" }, { status: 500 });
  }
}

