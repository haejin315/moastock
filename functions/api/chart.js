// GET /api/chart?symbol=005930.KS&tf=minute|day|week|month
// 종목 상세 페이지의 캔들차트 데이터 (야후 v8 chart OHLCV 프록시).
// 타임프레임은 화이트리스트로 고정한다 - 임의 range/interval 프록시가 되지 않게.
import { json, bad, cached, fetchUpstream } from "./_utils.js";

const TIMEFRAMES = {
  minute: { range: "5d", interval: "5m", cache: 60 },    // 분봉(5분)
  day: { range: "1y", interval: "1d", cache: 300 },      // 일봉
  week: { range: "5y", interval: "1wk", cache: 3600 },   // 주봉
  month: { range: "max", interval: "1mo", cache: 3600 }, // 월봉
};

export async function onRequestGet(context) {
  const params = new URL(context.request.url).searchParams;
  const symbol = (params.get("symbol") || "").trim();
  const tf = TIMEFRAMES[params.get("tf") || "day"];
  if (!symbol || symbol.length > 16) return bad("symbol이 필요합니다");
  if (!tf) return bad(`tf는 ${Object.keys(TIMEFRAMES).join("|")} 중 하나여야 합니다`);

  return cached(context, tf.cache, async () => {
    const url =
      `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}` +
      `?range=${tf.range}&interval=${tf.interval}&includePrePost=false`;
    const body = await (await fetchUpstream(url)).json();
    const result = body?.chart?.result?.[0];
    if (!result) return bad(body?.chart?.error?.description || "차트 데이터 없음", 502);
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
