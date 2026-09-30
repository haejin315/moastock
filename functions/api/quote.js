// GET /api/quote?symbols=005930.KS,^KS11,AAPL
// 야후 파이낸스 v8 chart를 심볼별로 병렬 조회해 시세 + 스파크라인을 한 번에 준다.
// (v7 quote 배치 API는 crumb 쿠키를 요구하게 되어 사용하지 않는다)
import { json, bad, cached, fetchUpstream } from "./_utils.js";

const MAX_SYMBOLS = 24;
const CACHE_SEC = 30;

async function quoteOne(symbol) {
  const url =
    `https://query1.finance.yahoo.com/v8/finance/chart/` +
    `${encodeURIComponent(symbol)}?range=1d&interval=5m&includePrePost=false`;
  const body = await (await fetchUpstream(url)).json();
  const result = body?.chart?.result?.[0];
  if (!result) throw new Error(body?.chart?.error?.description || "no data");
  const meta = result.meta || {};
  const closes = (result.indicators?.quote?.[0]?.close || []).filter(
    (v) => v !== null && v !== undefined,
  );
  const price = meta.regularMarketPrice ?? closes[closes.length - 1] ?? null;
  const prev = meta.chartPreviousClose ?? meta.previousClose ?? null;
  // 스파크라인은 40포인트면 충분 - 페이로드를 줄인다
  const step = Math.max(1, Math.floor(closes.length / 40));
  return {
    symbol: meta.symbol || symbol,
    currency: meta.currency || "",
    exchange: meta.fullExchangeName || "",
    marketState: meta.marketState || "",
    price,
    prevClose: prev,
    changePct:
      price !== null && prev ? Math.round(((price - prev) / prev) * 10000) / 100 : null,
    spark: closes.filter((_, i) => i % step === 0),
    time: meta.regularMarketTime || null,
  };
}

export async function onRequestGet(context) {
  const symbols = (new URL(context.request.url).searchParams.get("symbols") || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .slice(0, MAX_SYMBOLS);
  if (!symbols.length) return bad("symbols 파라미터가 필요합니다");

  return cached(context, CACHE_SEC, async () => {
    const settled = await Promise.allSettled(symbols.map(quoteOne));
    const quotes = [];
    const errors = {};
    settled.forEach((r, i) => {
      if (r.status === "fulfilled") quotes.push(r.value);
      else errors[symbols[i]] = String(r.reason?.message || r.reason);
    });
    return json({ quotes, errors }, { maxAge: CACHE_SEC });
  });
}
