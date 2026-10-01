// GET /api/chart?symbol=005930.KS&tf=minute|day|week|month
// 종목 상세 페이지의 캔들차트 데이터 (야후 v8 chart OHLCV 프록시).
// 타임프레임은 화이트리스트로 고정한다 - 임의 range/interval 프록시가 되지 않게.
import { json, bad, cached, fetchUpstream } from "./_utils.js";

// windowSec: before(과거 페이징) 요청 시 한 번에 가져올 구간.
// 분봉은 야후가 최근 ~60일까지만 제공하므로 그 밖은 빈 응답으로 끝난다.
const TIMEFRAMES = {
  minute: { range: "5d", interval: "5m", cache: 60, windowSec: 8 * 86400 },
  day: { range: "1y", interval: "1d", cache: 300, windowSec: 370 * 86400 },
  week: { range: "5y", interval: "1wk", cache: 3600, windowSec: 5 * 370 * 86400 },
  month: { range: "max", interval: "1mo", cache: 3600, windowSec: 0 }, // 이미 전체
};

export async function onRequestGet(context) {
  const params = new URL(context.request.url).searchParams;
  const symbol = (params.get("symbol") || "").trim();
  const tf = TIMEFRAMES[params.get("tf") || "day"];
  const before = parseInt(params.get("before") || "0", 10) || 0;
  if (!symbol || symbol.length > 16) return bad("symbol이 필요합니다");
  if (!tf) return bad(`tf는 ${Object.keys(TIMEFRAMES).join("|")} 중 하나여야 합니다`);
  if (before && !tf.windowSec) return json({ symbol, candles: [], exhausted: true });

  return cached(context, tf.cache, async () => {
    // before가 있으면 range 대신 period1/period2로 그 이전 구간을 페이징한다
    const span = before
      ? `period1=${Math.max(0, before - tf.windowSec)}&period2=${before}`
      : `range=${tf.range}`;
    const url =
      `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}` +
      `?${span}&interval=${tf.interval}&includePrePost=false`;
    let body;
    try {
      body = await (await fetchUpstream(url)).json();
    } catch (err) {
      // 과거 페이징에서 범위 밖(예: 분봉 60일 초과)은 에러가 아니라 "소진"으로 응답
      if (before) return json({ symbol, candles: [], exhausted: true }, { maxAge: tf.cache });
      throw err;
    }
    const result = body?.chart?.result?.[0];
    if (!result) {
      if (before) return json({ symbol, candles: [], exhausted: true }, { maxAge: tf.cache });
      return bad(body?.chart?.error?.description || "차트 데이터 없음", 502);
    }
    const ts = result.timestamp || [];
    const q = result.indicators?.quote?.[0] || {};
    const candles = [];
    for (let i = 0; i < ts.length; i++) {
      const [o, h, l, c] = [q.open?.[i], q.high?.[i], q.low?.[i], q.close?.[i]];
      if (o === null || o === undefined || c === null || c === undefined) continue;
      candles.push({
        time: ts[i],
        open: o, high: h ?? Math.max(o, c), low: l ?? Math.min(o, c), close: c,
        volume: q.volume?.[i] ?? 0,
      });
    }
    return json(
      { symbol, tf: params.get("tf") || "day", currency: result.meta?.currency || "", candles },
      { maxAge: tf.cache },
    );
  });
}
