import test, { beforeEach } from "node:test";
import assert from "node:assert/strict";
import { bodyOf, ctx, installFakeCache, mockFetch } from "./helpers.mjs";
import { decodeEntities, json, bad } from "../src/api/_utils.js";
import * as dart from "../src/api/dart.js";
import * as quote from "../src/api/quote.js";
import * as stockfeed from "../src/api/stockfeed.js";
import * as news from "../src/api/news.js";
import * as screener from "../src/api/screener.js";
import * as chart from "../src/api/chart.js";

beforeEach(() => installFakeCache());

// ---- _utils ------------------------------------------------------------------

test("decodeEntities: 이름·숫자 엔티티를 풀고 &amp;는 한 번만 푼다", () => {
  assert.equal(decodeEntities("마이크론 &quot;메모리&quot; &amp; 삼성"), '마이크론 "메모리" & 삼성');
  assert.equal(decodeEntities("&#39;AI&#39; &#x2026; &lt;b&gt;"), "'AI' … <b>");
  assert.equal(decodeEntities("&amp;quot;"), "&quot;");   // 이중 해석 금지
});

test("json/bad: 캐시 헤더와 상태 코드", async () => {
  assert.equal(json({}, { maxAge: 60 }).headers.get("Cache-Control"), "public, max-age=60");
  assert.equal(json({}).headers.get("Cache-Control"), "no-store");
  const r = await bodyOf(bad("잘못된 요청"));
  assert.equal(r.status, 400);
  assert.equal(r.data.error, "잘못된 요청");
});

// ---- /api/dart ---------------------------------------------------------------

test("dart: 키가 없으면 원본을 부르지 않고 key_missing", async () => {
  const calls = mockFetch([]);
  const r = await bodyOf(await dart.onRequestGet(ctx("/api/dart")));
  assert.equal(r.data.error, "key_missing");
  assert.equal(calls.length, 0);
});

test("dart: 목록을 화면용 필드로 바꾸고 키는 응답에 노출하지 않는다", async () => {
  const calls = mockFetch([["opendart.fss.or.kr/api/list.json", () => ({
    status: "000", total_page: 3,
    list: [{ corp_name: "삼성전자", corp_cls: "Y", report_nm: "주요사항보고서", rcept_dt: "20261006",
             rcept_no: "20261006000123" }],
  })]]);
  const resp = await dart.onRequestGet(ctx("/api/dart?page=2", { env: { DART_API_KEY: "SECRET-KEY" } }));
  const text = await resp.clone().text();
  const r = await bodyOf(resp);
  assert.equal(r.status, 200);
  assert.deepEqual(r.data.items[0], {
    corp: "삼성전자", market: "유가", title: "주요사항보고서", date: "20261006",
    url: "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261006000123",
  });
  assert.equal(r.data.totalPages, 3);
  assert.match(calls[0], /page_no=2/);
  assert.ok(!text.includes("SECRET-KEY"), "API 키가 응답 본문에 있으면 안 된다");
});

test("dart: 원본 연결 실패는 원인이 담긴 502 (키 미포함)", async () => {
  mockFetch([["opendart", () => new Error("handshake failure")]]);
  const resp = await dart.onRequestGet(ctx("/api/dart", { env: { DART_API_KEY: "SECRET-KEY" } }));
  const r = await bodyOf(resp);
  assert.equal(r.status, 502);
  assert.match(r.data.error, /handshake failure/);
  assert.ok(!JSON.stringify(r.data).includes("SECRET-KEY"));
});

test("dart: OpenDART 오류 코드는 502로 전달", async () => {
  mockFetch([["opendart", () => ({ status: "020", message: "요청 제한을 초과하였습니다." })]]);
  const r = await bodyOf(await dart.onRequestGet(ctx("/api/dart", { env: { DART_API_KEY: "k" } })));
  assert.equal(r.status, 502);
  assert.match(r.data.error, /020/);
});

// ---- /api/quote --------------------------------------------------------------

function yahoo(symbol, closes, { price, prev }) {
  return { chart: { result: [{
    meta: { symbol, currency: "KRW", fullExchangeName: "KSE", marketState: "CLOSED",
            regularMarketPrice: price, chartPreviousClose: prev, regularMarketTime: 1790798401 },
    indicators: { quote: [{ close: closes }] },
  }] } };
}

test("quote: 등락률 계산, 스파크라인은 40점 이하이고 마지막 점(현재가)을 포함", async () => {
  const closes = Array.from({ length: 79 }, (_, i) => 100 + i);
  closes[10] = null;                                     // 결측은 건너뛴다
  mockFetch([["/v8/finance/chart/", () => yahoo("005930.KS", closes, { price: 178, prev: 160 })]]);
  const r = await bodyOf(await quote.onRequestGet(ctx("/api/quote?symbols=005930.KS")));
  const q = r.data.quotes[0];
  assert.equal(q.changePct, 11.25);
  assert.ok(q.spark.length <= 40, `스파크라인 ${q.spark.length}점`);
  assert.equal(q.spark.at(-1), 178);
  assert.ok(!q.spark.includes(null));
});

test("quote: 한 종목 실패가 전체를 막지 않는다", async () => {
  mockFetch([
    ["/chart/AAPL", () => yahoo("AAPL", [1, 2], { price: 2, prev: 1 })],
    ["/chart/BAD", () => new Response("nope", { status: 404 })],
  ]);
  const r = await bodyOf(await quote.onRequestGet(ctx("/api/quote?symbols=AAPL,BAD")));
  assert.equal(r.data.quotes.length, 1);
  assert.ok(r.data.errors.BAD);
});

test("quote: symbols 없으면 400", async () => {
  mockFetch([]);
  assert.equal((await quote.onRequestGet(ctx("/api/quote"))).status, 400);
});

// ---- /api/stockfeed ----------------------------------------------------------

test("stockfeed: 뉴스 제목의 태그 제거 + 엔티티 디코딩, 기사 URL 구성", async () => {
  mockFetch([["/api/news/stock/005930", () => ([{ items: [{
    officeId: "014", articleId: "0005501191", officeName: "파이낸셜뉴스", datetime: "202610011543",
    title: "마이크론 &quot;메모리 수급&quot;…<b>삼성</b>",
  }] }])]]);
  const r = await bodyOf(await stockfeed.onRequestGet(ctx("/api/stockfeed?code=005930&kind=news")));
  assert.equal(r.data.items[0].title, '마이크론 "메모리 수급"…삼성');
  assert.equal(r.data.items[0].url, "https://n.news.naver.com/mnews/article/014/0005501191");
});

test("stockfeed: 공시 제목도 디코딩", async () => {
  mockFetch([["/disclosure", () => ([{ title: "A&amp;B 주요사항", datetime: "2026-09-28T06:51:03", author: "KOSCOM" }])]]);
  const r = await bodyOf(await stockfeed.onRequestGet(ctx("/api/stockfeed?code=005930&kind=disclosure")));
  assert.equal(r.data.items[0].title, "A&B 주요사항");
});

test("stockfeed: 입력 검증 (6자리 코드, kind 허용값)", async () => {
  mockFetch([]);
  assert.equal((await stockfeed.onRequestGet(ctx("/api/stockfeed?code=5930"))).status, 400);
  assert.equal((await stockfeed.onRequestGet(ctx("/api/stockfeed?code=005930&kind=x"))).status, 400);
});

// ---- /api/news ---------------------------------------------------------------

test("news: RSS 파싱 (CDATA, 엔티티), 허용 목록 밖 피드는 거부", async () => {
  const rss = `<rss><channel>
    <item><title><![CDATA[코스피 &quot;7000&quot; 돌파]]></title><link>https://x.test/1</link>
      <pubDate>Thu, 01 Oct 2026 07:00:00 GMT</pubDate></item>
    <item><title>링크 없는 항목</title></item>
  </channel></rss>`;
  mockFetch([["hankyung.com", () => rss]]);
  const r = await bodyOf(await news.onRequestGet(ctx("/api/news?src=hk")));
  assert.equal(r.data.items.length, 1);
  assert.equal(r.data.items[0].title, '코스피 "7000" 돌파');
  assert.equal((await news.onRequestGet(ctx("/api/news?src=evil"))).status, 400);   // SSRF 방지
});

// ---- /api/screener -----------------------------------------------------------

test("screener: 단위 환산(거래대금 백만원→원, 시총 억원→원)과 ETF 제외", async () => {
  mockFetch([["/marketValue/KOSPI", () => ({
    totalCount: 2,
    stocks: [
      { itemCode: "005930", stockEndType: "stock", closePrice: "276,000", fluctuationsRatio: "2.79",
        accumulatedTradingVolume: "1,000", accumulatedTradingValue: "3,600,000", marketValue: "16,136,000" },
      { itemCode: "069500", stockEndType: "etf", closePrice: "1" },
    ],
  })]]);
  const r = await bodyOf(await screener.onRequestGet(ctx("/api/screener?market=KOSPI")));
  assert.equal(r.data.count, 1);
  const s = r.data.quotes[0];
  assert.equal(s.price, 276000);
  assert.equal(s.value, 3.6e12);
  assert.equal(s.marketCap, 1.6136e15);
  assert.equal((await screener.onRequestGet(ctx("/api/screener?market=NYSE"))).status, 400);
});

test("screener: 미국(나스닥 API) - 기호를 야후 형식으로, $·% 떼고 숫자로", async () => {
  mockFetch([["api.nasdaq.com", (url) => ({ data: { rows: url.includes("exchange=nyse") ? [
    { symbol: "BRK/B", lastsale: "$480.10", pctchange: "-0.5%", volume: "1000", marketCap: "1000000000000" },
  ] : url.includes("exchange=nasdaq") ? [
    { symbol: "AAPL", lastsale: "$336.67", pctchange: "0.91%", volume: "2,000", marketCap: "4913422663680" },
    { symbol: "XXXX", lastsale: "NA", pctchange: "", volume: "", marketCap: "" },
  ] : [] } })]]);
  const r = await bodyOf(await screener.onRequestGet(ctx("/api/screener?market=US")));
  const by = Object.fromEntries(r.data.quotes.map((q) => [q.code, q]));
  assert.equal(r.data.count, 2, "시세 없는 행은 뺀다");
  assert.equal(by["BRK-B"].price, 480.1);
  assert.equal(by.AAPL.change, 0.91);
  assert.equal(by.AAPL.value, Math.round(336.67 * 2000));
});

test("screener: 코인 - 업비트·빗썸·바이낸스, 바이낸스는 원화 환산, 김치 프리미엄", async () => {
  const krwTicker = (price) => (url) => {
    assert.ok(!url.includes("BTC-ETH"), "원화마켓만 조회");
    return [{ market: "KRW-BTC", trade_price: price, signed_change_rate: -0.0032, acc_trade_volume_24h: 10,
              acc_trade_price_24h: 1.5e12 }];
  };
  mockFetch([
    ["/v8/finance/chart/KRW%3DX", () => ({ chart: { result: [{ meta: { regularMarketPrice: 1400 } }] } })],
    ["api.upbit.com/v1/market/all", () => [{ market: "KRW-BTC" }, { market: "BTC-ETH" }]],
    ["api.upbit.com/v1/ticker", krwTicker(142800000)],
    ["api.bithumb.com/v1/market/all", () => [{ market: "KRW-BTC" }]],
    ["api.bithumb.com/v1/ticker", krwTicker(140000000)],
    ["data-api.binance.vision", () => [
      { symbol: "BTCUSDT", lastPrice: "100000", priceChangePercent: "-1.389", volume: "2", quoteVolume: "200000" },
      { symbol: "ETHBTC", lastPrice: "0.03", priceChangePercent: "0", volume: "1", quoteVolume: "1" },
    ]],
  ]);
  const r = await bodyOf(await screener.onRequestGet(ctx("/api/screener?market=COIN")));
  const by = Object.fromEntries(r.data.quotes.map((q) => [`${q.exchange}:${q.code}`, q]));
  assert.equal(r.data.usdkrw, 1400);
  assert.equal(by["BINANCE:BTC"].price, 140000000, "100,000달러 × 1,400원");
  assert.equal(by["BINANCE:BTC"].value, 280000000);
  assert.equal(by["UPBIT:BTC"].premium, 2, "142.8백만 / 140백만 - 1 = 2%");
  assert.equal(by["BITHUMB:BTC"].premium, 0);
  assert.equal(by["UPBIT:BTC"].change, -0.32);
  assert.ok(!by["BINANCE:ETH"], "USDT 마켓만");
});

// ---- /api/chart --------------------------------------------------------------

test("chart: 타임프레임 화이트리스트, OHLCV 결측 캔들 제외", async () => {
  mockFetch([["/v8/finance/chart/", () => ({ chart: { result: [{
    meta: { currency: "KRW" }, timestamp: [1, 2, 3],
    indicators: { quote: [{ open: [10, null, 12], high: [11, 1, null], low: [9, 1, 11],
                            close: [10.5, 1, 12.5], volume: [100, 1, null] }] },
  }] } })]]);
  const r = await bodyOf(await chart.onRequestGet(ctx("/api/chart?symbol=005930.KS&tf=day")));
  assert.equal(r.data.candles.length, 2);
  assert.deepEqual(r.data.candles[1], { time: 3, open: 12, high: 12.5, low: 11, close: 12.5, volume: 0 });
  assert.equal((await chart.onRequestGet(ctx("/api/chart?symbol=X&tf=year"))).status, 400);
});
