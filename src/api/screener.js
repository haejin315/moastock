// GET /api/screener?market=KOSPI|KOSDAQ|US|COIN
// 장중 실시간 갱신용: 전 종목의 시세/등락률/거래량/거래대금/시총만 가볍게 준다.
// PER 등 펀더멘털은 일일 스냅샷(/data/snapshot.json, us.json, jp.json, coins.json)이 담당하고,
// 클라이언트가 종목코드로 병합한다.
//   KOSPI·KOSDAQ: 네이버 모바일 증권 (페이지 팬아웃 10~19 서브요청)
//   US: 나스닥 스크리너 API (나스닥·뉴욕·아멕스 3번)
//   COIN: 업비트·빗썸 원화마켓 + 바이낸스(원/달러 환산) + 김치 프리미엄 (서브요청 10개 안팎)
// 일본은 장중 갱신 없이 일일 스냅샷만 쓴다. 모두 60초 엣지 캐시.
import { json, bad, cached, fetchUpstream } from "./_utils.js";

const CACHE_SEC = 60;
const PAGE_SIZE = 100;
const MAX_PAGES = 25;
const MARKETS = new Set(["KOSPI", "KOSDAQ", "US", "COIN"]);
const BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36";

export async function onRequestGet(context) {
  const market = (new URL(context.request.url).searchParams.get("market") || "").toUpperCase();
  if (!MARKETS.has(market)) return bad("market은 KOSPI, KOSDAQ, US, COIN 중 하나여야 합니다");
  if (market === "US") return cached(context, CACHE_SEC, () => usQuotes());
  if (market === "COIN") return cached(context, CACHE_SEC, () => coinQuotes());

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

// 야후 기호로 맞춘다 (BRK/B → BRK-B) - 일일 스냅샷 us.json 의 code 와 같게
const usSymbol = (s) => String(s).trim().replace(/[/.]/g, "-").toUpperCase();

async function usQuotes() {
  const rows = [];
  for (const ex of ["nasdaq", "nyse", "amex"]) {
    // 나스닥 API는 브라우저가 아닌 User-Agent 요청을 응답 없이 끊는다
    const resp = await fetch(`https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true&exchange=${ex}`, {
      headers: { "User-Agent": BROWSER_UA, Accept: "application/json", "Accept-Language": "en-US,en;q=0.9" },
      cf: { cacheTtl: 30 },
    });
    if (!resp.ok) throw new Error(`upstream ${resp.status}`);
    const body = await resp.json();
    for (const r of body?.data?.rows || []) {
      const price = toNum(String(r.lastsale || "").replace("$", ""));
      if (price === null) continue;
      rows.push({
        code: usSymbol(r.symbol), price, change: toNum(String(r.pctchange || "").replace("%", "")),
        volume: toNum(r.volume), marketCap: toNum(r.marketCap),
        value: price && toNum(r.volume) ? Math.round(price * toNum(r.volume)) : null,
      });
    }
  }
  return json({ market: "US", count: rows.length, quotes: rows }, { maxAge: CACHE_SEC });
}

// 업비트·빗썸 원화마켓 (두 거래소 공개 API 형식이 같다)
async function krwExchange(base, label) {
  const markets = (await (await fetchUpstream(`${base}/v1/market/all?isDetails=false`)).json())
    .map((m) => m.market).filter((m) => m.startsWith("KRW-"));
  const rows = [];
  for (let i = 0; i < markets.length; i += 100) {
    const part = markets.slice(i, i + 100).join(",");
    for (const t of await (await fetchUpstream(`${base}/v1/ticker?markets=${part}`)).json()) {
      rows.push({
        exchange: label, code: t.market.slice(4), price: t.trade_price,
        change: Math.round(t.signed_change_rate * 10000) / 100,
        volume: t.acc_trade_volume_24h, value: Math.round(t.acc_trade_price_24h),
      });
    }
  }
  return rows;
}

// 바이낸스 USDT 마켓 (지역 제한 없는 공개 시세 미러) - 원/달러로 원화 환산.
// 전 종목 응답은 2MB 가까워 Worker CPU 한도에 걸릴 수 있어, 국내 거래소에도 있는 코인만 100개씩 묻는다
// (바이낸스에만 있는 코인은 일일 스냅샷 값을 쓴다).
async function binanceRows(usdkrw, codes) {
  const headers = { "User-Agent": BROWSER_UA, Accept: "application/json" };
  // 1) 전 종목 가격만 (가볍다) - 바이낸스에 실제 있는 기호를 고르는 데 쓴다 (없는 기호가 섞이면 묶음 전체가 400)
  const priceResp = await fetch("https://data-api.binance.vision/api/v3/ticker/price", { headers, cf: { cacheTtl: 30 } });
  if (!priceResp.ok) throw new Error(`binance ${priceResp.status}`);
  const listed = new Set((await priceResp.json()).map((t) => t.symbol));
  const syms = [...codes].map((c) => `${c}USDT`).filter((s) => listed.has(s));
  const rows = [];
  // 2) 그중 국내 거래소에도 있는 코인만 24시간 통계를 100개씩
  for (let i = 0; i < syms.length; i += 100) {
    const q = encodeURIComponent(JSON.stringify(syms.slice(i, i + 100)));
    const resp = await fetch(`https://data-api.binance.vision/api/v3/ticker/24hr?symbols=${q}&type=MINI`,
      { headers, cf: { cacheTtl: 30 } });
    if (!resp.ok) { rows.error = `binance ${resp.status}`; continue; }
    for (const t of await resp.json()) {
      const usd = Number(t.lastPrice), open = Number(t.openPrice);
      if (!(usd > 0)) continue;
      const krw = usd * usdkrw;
      rows.push({
        exchange: "BINANCE", code: t.symbol.slice(0, -4), priceUsd: usd,
        price: krw < 100 ? Math.round(krw * 1e4) / 1e4 : Math.round(krw),
        change: open > 0 ? Math.round((usd / open - 1) * 10000) / 100 : null,
        volume: Number(t.volume), value: Math.round(Number(t.quoteVolume) * usdkrw),
      });
    }
  }
  return rows;
}

async function usdKrw() {
  const body = await (await fetchUpstream(
    "https://query1.finance.yahoo.com/v8/finance/chart/KRW%3DX?range=1d&interval=1d")).json();
  return body?.chart?.result?.[0]?.meta?.regularMarketPrice;
}

// 코인: 업비트·빗썸·바이낸스 + 국내 거래소의 김치 프리미엄(바이낸스 원화 환산가 대비 %)
async function coinQuotes() {
  const errors = {};
  const usdkrw = await usdKrw().catch((e) => { errors.usdkrw = String(e.message || e); return null; });
  const [up, bt] = await Promise.all([
    krwExchange("https://api.upbit.com", "UPBIT"),
    krwExchange("https://api.bithumb.com", "BITHUMB"),
  ]);
  let bn = [];
  if (usdkrw) {
    bn = await binanceRows(usdkrw, new Set([...up, ...bt].map((r) => r.code)))
      .catch((e) => { errors.BINANCE = String(e.message || e); return []; });
    if (bn.error) errors.BINANCE = bn.error;
  }
  const bnBy = new Map(bn.map((r) => [r.code, r]));
  for (const r of [...up, ...bt]) {
    const b = bnBy.get(r.code);
    if (b && b.priceUsd && usdkrw) r.premium = Math.round((r.price / (b.priceUsd * usdkrw) - 1) * 10000) / 100;
  }
  const quotes = [...up, ...bt, ...bn];
  return json({ market: "COIN", usdkrw, count: quotes.length, quotes, errors }, { maxAge: CACHE_SEC });
}

function toNum(text) {
  if (text === null || text === undefined) return null;
  const n = parseFloat(String(text).replace(/,/g, ""));
  return Number.isFinite(n) ? n : null;
}
