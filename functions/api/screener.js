// GET /api/screener?market=KOSPI|KOSDAQ
// 장중 실시간 갱신용: 전 종목의 시세/등락률/거래량/거래대금/시총만 가볍게 준다.
// PER 등 펀더멘털은 일일 스냅샷(/data/snapshot.json)이 담당하고, 클라이언트가
// 종목코드로 병합한다. 페이지네이션 팬아웃(10~19 서브요청)이 있어 60초 엣지 캐시.
import { json, bad, cached, fetchUpstream } from "./_utils.js";

const CACHE_SEC = 60;
const PAGE_SIZE = 100;
const MAX_PAGES = 25;

export async function onRequestGet(context) {
  const market = (new URL(context.request.url).searchParams.get("market") || "").toUpperCase();
  if (market !== "KOSPI" && market !== "KOSDAQ") {
    return bad("market은 KOSPI 또는 KOSDAQ 이어야 합니다");
  }

  return cached(context, CACHE_SEC, async () => {
    const rows = [];
    let total = Infinity;
    for (let page = 1; page <= MAX_PAGES && (page - 1) * PAGE_SIZE < total; page++) {
      const body = await (
        await fetchUpstream(
          `https://m.stock.naver.com/api/stocks/marketValue/${market}?page=${page}&pageSize=${PAGE_SIZE}`,
        )
      ).json();
      total = Number(body.totalCount || 0);
      for (const s of body.stocks || []) {
        if (s.stockEndType !== "stock") continue;
        rows.push({
          code: s.itemCode,
          price: toNum(s.closePrice),
          change: toNum(s.fluctuationsRatio),
          volume: toNum(s.accumulatedTradingVolume),
          value: toNum(s.accumulatedTradingValue) * 1e6,     // 백만원 → 원
          marketCap: toNum(s.marketValue) * 1e8,             // 억원 → 원
        });
      }
      if (!(body.stocks || []).length) break;
    }
    return json({ market, count: rows.length, quotes: rows }, { maxAge: CACHE_SEC });
  });
}

function toNum(text) {
  if (text === null || text === undefined) return null;
  const n = parseFloat(String(text).replace(/,/g, ""));
  return Number.isFinite(n) ? n : null;
}
