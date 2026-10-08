/* 종목 상세: 캔들차트(분/일/주/월), 지표, 공시, 뉴스, 토론방.
   URL: 국내 /stock.html?code=005930
        해외·코인 /stock.html?market=US&symbol=AAPL · market=JP&symbol=7203.T · market=COIN&symbol=BTC-KRW
        (해외·코인은 공시·뉴스 없이 차트·지표·토론방만) */
"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const params = new URLSearchParams(location.search);
const MARKETS = {
  KR: { data: "/data/snapshot.json", currency: "KRW", source: "네이버증권" },
  US: { data: "/data/us.json", currency: "USD", source: "야후 파이낸스" },
  JP: { data: "/data/jp.json", currency: "JPY", source: "야후 파이낸스" },
  COIN: { data: "/data/coins.json", currency: "KRW", source: "업비트·코인게코" },
};
const MARKET_LABEL = { KOSPI: "코스피", KOSDAQ: "코스닥", NASDAQ: "나스닥", NYSE: "뉴욕", AMEX: "아멕스", TSE: "도쿄",
  UPBIT: "업비트", GLOBAL: "해외 거래소" };
const market = MARKETS[(params.get("market") || "KR").toUpperCase()] ? (params.get("market") || "KR").toUpperCase() : "KR";
const M = MARKETS[market];
// 국내는 6자리 종목코드, 해외·코인은 야후 기호 (토론방 글도 이 값으로 묶는다)
const code = market === "KR" ? (params.get("code") || "").trim() : (params.get("symbol") || "").trim().toUpperCase();
if (market === "KR" ? !/^\d{6}$/.test(code) : !/^[A-Z0-9][A-Z0-9.-]{0,14}$/.test(code)) {
  document.body.innerHTML = '<p style="padding:40px">잘못된 종목코드입니다. <a href="/screener.html">스크리너로</a></p>';
  throw new Error("bad code");
}

let stock = null;   // snapshot 행
let snapshotAt = "";
let symbol = market === "KR" ? code + ".KS" : code;

const fmtPrice = (v) => {
  if (v === null || v === undefined || !Number.isFinite(v)) return "-";
  if (M.currency === "USD") return "$" + v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (M.currency === "JPY") return "¥" + Math.round(v).toLocaleString("ko-KR");
  if (Math.abs(v) < 100 && v % 1) return v.toLocaleString("ko-KR", { maximumFractionDigits: Math.abs(v) < 1 ? 4 : 2 });
  return v.toLocaleString("ko-KR");
};
const fmtBig = (v) => {
  if (v === null || v === undefined || !Number.isFinite(v)) return "-";
  const a = Math.abs(v), sign = v < 0 ? "-" : "";
  const unit = M.currency === "USD" ? " 달러" : M.currency === "JPY" ? "엔" : "";
  if (a >= 1e12) return sign + (a / 1e12).toFixed(1) + "조" + unit;
  if (a >= 1e8) return sign + Math.round(a / 1e8).toLocaleString("ko-KR") + "억" + unit;
  return M.currency === "USD" ? "$" + Math.round(v).toLocaleString("en-US") : sign + Math.round(a).toLocaleString("ko-KR") + unit;
};
const fmtRatio = (v) => v === null || v === undefined || Number.isNaN(v) ? "-" : v.toFixed(2);
// 서버 시각(UTC)을 한국 시간 "YYYY-MM-DD HH:mm"으로
const fmtKst = (ms) => new Date(ms + 9 * 3600e3).toISOString().replace("T", " ").slice(0, 16);
const MARKET_STATE = { PRE: "장 시작 전", REGULAR: "장중", POST: "장 마감 후", CLOSED: "장 마감" };
const chgClass = (v) => v === null || v === undefined ? "flat" : v > 0 ? "up" : v < 0 ? "down" : "flat";

// ---- 관심종목 ---------------------------------------------------------------

function loadWatch() {
  try { return new Set(JSON.parse(localStorage.getItem("moastock.watchlist") || "[]")); }
  catch (e) { return new Set(); }
}
let watch = loadWatch();
function paintWatch() {
  $("#watch-toggle").classList.toggle("on", watch.has(symbol));
}
$("#watch-toggle").addEventListener("click", () => {
  watch.has(symbol) ? watch.delete(symbol) : watch.add(symbol);
  try { localStorage.setItem("moastock.watchlist", JSON.stringify([...watch])); } catch (e) {}
  paintWatch();
});

// ---- 기본 정보 ---------------------------------------------------------------

async function loadInfo() {
  try {
    const body = await (await fetch(M.data)).json();
    stock = body.stocks.find((s) => market === "KR" ? s.code === code
      : market === "COIN" ? `${s.code}-KRW` === code : s.symbol === code) || null;
    snapshotAt = body.generatedAt || "";
  } catch (e) { /* 스냅샷 없이도 차트는 동작 */ }
  if (stock) {
    if (market === "KR") symbol = code + (stock.market === "KOSDAQ" ? ".KQ" : ".KS");
    const name = stock.nameKo || stock.name;
    document.title = `${name} - 모아스톡`;
    $("#stock-title").firstChild.textContent = name;
    $("#stock-code").textContent = [stock.code, MARKET_LABEL[stock.market] || stock.market, stock.industry,
      stock.nameKo && stock.name !== stock.nameKo ? stock.name : null].filter(Boolean).join(" · ");
    renderFacts();
  } else {
    $("#stock-title").firstChild.textContent = code;
    $("#stock-code").textContent = code;
  }
  paintWatch();
  refreshQuote();
}

// livePrice가 있으면 시가총액을 현재가로 다시 계산한다(주식수 = 스냅샷 시총 ÷ 스냅샷 가격).
// 스냅샷 시총은 전일 종가 기준이라 그대로 쓰면 스크리너(실시간)와 숫자가 어긋난다.
function renderFacts(livePrice = null) {
  const s = stock;
  const shares = s.price && s.marketCap ? s.marketCap / s.price : null;
  const marketCap = livePrice && shares ? shares * livePrice : s.marketCap;
  if (market === "COIN") {
    $("#facts").innerHTML = [
      ["시가총액", fmtBig(marketCap)], ["시가총액 순위", s.rank ? `${s.rank}위` : "-"],
      ["24시간 거래대금", fmtBig(s.value)], ["52주 최고가", fmtPrice(s.high52w)], ["52주 최저가", fmtPrice(s.low52w)],
    ].map(([k, v]) => `<div class="fact"><div class="fact-k">${k}</div><div class="fact-v">${v}</div></div>`).join("");
    $("#facts-note").textContent = `코인 지표: ${M.source} ${String(snapshotAt).slice(0, 10)} 기준`;
    return;
  }
  const facts = [
    ["시가총액", fmtBig(marketCap)],
    ["주가수익비율(PER)", fmtRatio(s.per)], ["주가순자산비율(PBR)", fmtRatio(s.pbr)],
    ["주당순이익(EPS)", fmtPrice(s.eps)], ["주당순자산(BPS)", fmtPrice(s.bps)],
    ["자기자본이익률(ROE)", s.eps !== null && s.bps ? (s.eps / s.bps * 100).toFixed(1) + "%" : "-"],
    ["순이익(추정)", s.eps !== null && shares ? fmtBig(s.eps * shares) : "-"],
    ["순자산(추정)", s.bps !== null && shares ? fmtBig(s.bps * shares) : "-"],
    ["배당수익률", s.dividendYield !== null && s.dividendYield !== undefined ? s.dividendYield.toFixed(2) + "%" : "-"],
    ...(market === "KR" ? [["외국인보유비율", s.foreignRate !== null ? s.foreignRate.toFixed(2) + "%" : "-"]] : []),
    ["52주 최고가", fmtPrice(s.high52w)], ["52주 최저가", fmtPrice(s.low52w)],
  ];
  $("#facts").innerHTML = facts.map(([k, v]) =>
    `<div class="fact"><div class="fact-k">${k}</div><div class="fact-v">${v}</div></div>`).join("");
  $("#facts-note").textContent = `재무 지표: ${M.source} ${String(snapshotAt).slice(0, 10)} 스냅샷` +
    (livePrice && shares ? " · 시가총액은 현재가 기준" : "");
}

async function refreshQuote() {
  try {
    const body = await (await fetch(`/api/quote?symbols=${encodeURIComponent(symbol)}`)).json();
    const q = body.quotes?.[0];
    if (!q) return;
    $("#price").textContent = fmtPrice(q.price);
    const el = $("#price-chg");
    el.className = chgClass(q.changePct);
    el.textContent = (q.changePct > 0 ? "▲ " : q.changePct < 0 ? "▼ " : "") +
      `${Math.abs(q.changePct ?? 0).toFixed(2)}%  (전일 ${fmtPrice(q.prevClose)})`;
    $("#meta").textContent = [
      "Yahoo Finance",
      q.time ? fmtKst(q.time * 1000) + " 기준" : "",
      MARKET_STATE[q.marketState] || "",
    ].filter(Boolean).join(" · ");
    if (stock) renderFacts(q.price);
  } catch (e) { /* 유지 */ }
}

// ---- 차트 ---------------------------------------------------------------------

let chart = null, candleSeries = null, volumeSeries = null;
// 무한 히스토리: 보이는 범위가 왼쪽 끝(과거)에 닿으면 이전 구간을 추가 로드해
// 차트가 끊기지 않고 이어진다.
const chartState = { tf: "day", candles: [], loading: false, exhausted: false, epoch: 0 };

function ensureChart() {
  if (chart) return;
  const css = getComputedStyle(document.documentElement);
  chart = LightweightCharts.createChart($("#chart"), {
    autoSize: true,
    layout: {
      background: { type: "solid", color: "transparent" },
      textColor: css.getPropertyValue("--muted").trim() || "#7c8aa5",
      attributionLogo: false,   // 거래량 막대를 가려서 끄고, 출처는 푸터에 표기
    },
    grid: {
      vertLines: { color: css.getPropertyValue("--line").trim() || "#1e293b" },
      horzLines: { color: css.getPropertyValue("--line").trim() || "#1e293b" },
    },
    timeScale: { timeVisible: true, borderVisible: false, rightOffset: 4 },
    rightPriceScale: { borderVisible: false },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  });
  candleSeries = chart.addCandlestickSeries({
    upColor: "#f87171", wickUpColor: "#f87171", borderUpColor: "#f87171",     // 한국 관례: 상승=빨강
    downColor: "#60a5fa", wickDownColor: "#60a5fa", borderDownColor: "#60a5fa",
    // 원화 가격은 소수점 없이 천 단위 구분 (거래량 축은 기본 K/M 표기 유지)
    priceFormat: M.currency === "USD" || (stock && stock.price < 100)
      ? { type: "custom", minMove: 0.0001, formatter: (p) => fmtPrice(p) }
      : { type: "custom", minMove: 1, formatter: (p) => Math.round(p).toLocaleString("ko-KR") },
  });
  volumeSeries = chart.addHistogramSeries({
    priceFormat: { type: "volume" },
    priceScaleId: "vol",
  });
  chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  // 왼쪽(과거)으로 15개 캔들 이내로 접근하면 이전 구간을 당겨온다
  chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
    if (range && range.from < 15) loadOlder();
  });
}

function setSeriesData() {
  const candles = chartState.candles;
  candleSeries.setData(candles.map((c) => ({
    time: c.time, open: c.open, high: c.high, low: c.low, close: c.close,
  })));
  volumeSeries.setData(candles.map((c) => ({
    time: c.time, value: c.volume,
    color: c.close >= c.open ? "rgba(248,113,113,0.45)" : "rgba(96,165,250,0.45)",
  })));
}

const TF_LABEL = { minute: "5분봉", day: "일봉", week: "주봉", month: "월봉" };

function noteText(extra = "") {
  return `${TF_LABEL[chartState.tf]} · ${chartState.candles.length.toLocaleString("ko-KR")}개 캔들 · ` +
    `과거로 드래그하면 이전 데이터가 이어집니다${extra} · Yahoo Finance`;
}

async function loadChart(tf) {
  ensureChart();
  const epoch = ++chartState.epoch;      // 탭 전환 시 이전 요청/페이징 무효화
  chartState.tf = tf;
  chartState.candles = [];
  chartState.loading = false;
  chartState.exhausted = tf === "month"; // 월봉은 이미 전체 구간
  $("#chart-note").textContent = "차트 불러오는 중…";
  try {
    const body = await (await fetch(`/api/chart?symbol=${encodeURIComponent(symbol)}&tf=${tf}`)).json();
    if (epoch !== chartState.epoch) return;
    chartState.candles = body.candles || [];
    setSeriesData();
    // 양 끝에 여백을 둬 첫/마지막 축 라벨이 잘리지 않게 한다
    chart.timeScale().setVisibleLogicalRange({ from: -3, to: chartState.candles.length + 3 });
    $("#chart-note").textContent = noteText();
  } catch (e) {
    $("#chart-note").textContent = "차트를 불러오지 못했습니다";
  }
}

async function loadOlder() {
  if (chartState.loading || chartState.exhausted || !chartState.candles.length) return;
  chartState.loading = true;
  const epoch = chartState.epoch;
  const first = chartState.candles[0].time;
  try {
    const body = await (await fetch(
      `/api/chart?symbol=${encodeURIComponent(symbol)}&tf=${chartState.tf}&before=${first}`,
    )).json();
    if (epoch !== chartState.epoch) return;
    const older = (body.candles || []).filter((c) => c.time < first);
    if (body.exhausted || !older.length) {
      chartState.exhausted = true;
      $("#chart-note").textContent = noteText(" (전체 구간 끝)");
      return;
    }
    // 데이터를 앞에 붙이면 논리 인덱스가 밀리므로, 보이던 범위를 되돌려
    // 화면이 점프하지 않게 한다
    const view = chart.timeScale().getVisibleLogicalRange();
    chartState.candles = older.concat(chartState.candles);
    setSeriesData();
    if (view) {
      chart.timeScale().setVisibleLogicalRange({
        from: view.from + older.length,
        to: view.to + older.length,
      });
    }
    $("#chart-note").textContent = noteText();
  } catch (e) { /* 다음 스크롤에서 재시도 */ }
  finally {
    if (epoch === chartState.epoch) chartState.loading = false;
  }
}

$("#tf-tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-tf]");
  if (!btn) return;
  document.querySelectorAll("#tf-tabs button").forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  loadChart(btn.dataset.tf);
});

// ---- 공시 / 뉴스 -----------------------------------------------------------------

function timeAgo(dt) {
  const t = Date.parse(dt);
  if (Number.isNaN(t)) {
    // 네이버 뉴스 포맷 YYYYMMDDHHmm
    const m = String(dt).match(/^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})/);
    if (!m) return "";
    return `${m[1]}.${m[2]}.${m[3]} ${m[4]}:${m[5]}`;
  }
  const mins = Math.max(0, Math.round((Date.now() - t) / 60000));
  if (mins < 60) return `${mins}분 전`;
  if (mins < 1440) return `${Math.round(mins / 60)}시간 전`;
  return new Date(t).toLocaleDateString("ko-KR");
}

async function loadFeed(kind, listId) {
  const el = $(listId);
  try {
    const body = await (await fetch(`/api/stockfeed?code=${code}&kind=${kind}`)).json();
    if (!body.items?.length) { el.innerHTML = '<li class="muted">최근 항목이 없습니다</li>'; return; }
    el.innerHTML = body.items.map((n) => `<li>
      <a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.title)}</a>
      <div class="meta">${n.press ? `<span>${esc(n.press)}</span>` : ""}${n.author ? `<span>${esc(n.author)}</span>` : ""}
        <span>${esc(timeAgo(n.datetime))}</span></div></li>`).join("");
  } catch (e) {
    el.innerHTML = '<li class="muted">불러오기 실패</li>';
  }
}

// ---- 토론방 -----------------------------------------------------------------------

// D1 datetime('now')는 "YYYY-MM-DD HH:MM:SS"(UTC)
function boardTime(raw) {
  const t = Date.parse(String(raw).replace(" ", "T") + "Z");
  return Number.isNaN(t) ? String(raw) : fmtKst(t);
}

async function loadBoard() {
  const el = $("#board-list");
  try {
    const body = await (await fetch(`/api/board?code=${encodeURIComponent(code)}`)).json();
    if (body.error === "board_unavailable") {
      el.innerHTML = '<li class="muted">토론방 저장소 준비 중입니다</li>';
      return;
    }
    $("#board-count").textContent = body.items.length ? `${body.items.length}개 글` : "";
    if (!body.items.length) {
      el.innerHTML = '<li class="muted">첫 글을 남겨보세요</li>';
      return;
    }
    el.innerHTML = body.items.map((p) => `<li data-id="${p.id}">
      <div class="board-head"><b>${esc(p.nick)}</b>
        <span class="muted">${esc(boardTime(p.created_at))}</span>
        ${p.deletable ? '<button class="board-del-open" type="button">삭제</button>' : ""}</div>
      <div class="board-body">${esc(p.body)}</div>
      <div class="board-del" hidden>
        <input type="password" placeholder="작성 시 비밀번호" minlength="4" maxlength="12" autocomplete="current-password">
        <button class="board-del-confirm" type="button">확인</button>
        <button class="board-del-cancel ghost" type="button">취소</button>
        <span class="board-del-msg"></span>
      </div></li>`).join("");
  } catch (e) {
    el.innerHTML = '<li class="muted">토론방 불러오기 실패</li>';
  }
}

// 삭제는 글 아래에 비밀번호 입력칸을 펼쳐서 처리한다 (브라우저 prompt 미사용)
$("#board-list").addEventListener("click", async (e) => {
  const li = e.target.closest("li[data-id]");
  if (!li) return;
  const panel = li.querySelector(".board-del");
  if (e.target.closest(".board-del-open")) {
    panel.hidden = !panel.hidden;
    if (!panel.hidden) panel.querySelector("input").focus();
    return;
  }
  if (e.target.closest(".board-del-cancel")) {
    panel.hidden = true;
    return;
  }
  if (!e.target.closest(".board-del-confirm")) return;
  const msg = panel.querySelector(".board-del-msg");
  const password = panel.querySelector("input").value;
  if (password.length < 4 || password.length > 12) {
    msg.textContent = "비밀번호는 4~12자입니다";
    return;
  }
  try {
    const resp = await fetch("/api/board", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: Number(li.dataset.id), password }),
    });
    const out = await resp.json();
    if (!resp.ok) throw new Error(out.error || "삭제 실패");
    loadBoard();
  } catch (err) {
    msg.textContent = err.message;
  }
});
$("#board-list").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.closest(".board-del input")) {
    e.target.closest(".board-del").querySelector(".board-del-confirm").click();
  }
});

$("#board-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = $("#board-body").value.trim();
  const password = $("#board-pw").value;
  const errEl = $("#board-error");
  if (!body) return;
  if (password.length < 4 || password.length > 12) {
    errEl.textContent = "비밀번호를 4~12자로 입력하세요 (글 삭제 시 필요)";
    errEl.hidden = false;
    return;
  }
  try {
    const resp = await fetch("/api/board", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code, nick: $("#board-nick").value.trim(), body, password }),
    });
    const out = await resp.json();
    if (!resp.ok) throw new Error(out.error || "등록 실패");
    $("#board-body").value = "";
    errEl.hidden = true;
    loadBoard();
  } catch (err) {
    errEl.textContent = err.message;
    errEl.hidden = false;
  }
});

// ---- 테마 / 시작 --------------------------------------------------------------------

$("#theme-toggle").addEventListener("click", () => {
  const root = document.documentElement;
  root.dataset.theme = root.dataset.theme === "light" ? "dark" : "light";
  try { localStorage.setItem("moastock.theme", root.dataset.theme); } catch (e) {}
});
try {
  const saved = localStorage.getItem("moastock.theme");
  if (saved) document.documentElement.dataset.theme = saved;
} catch (e) {}

loadInfo().then(() => {
  loadChart("day");
  if (market === "KR") {
    loadFeed("disclosure", "#disclosure-list");
    loadFeed("news", "#news-list");
  } else {
    $("#feeds").hidden = true;            // 해외·코인은 공시·뉴스를 수집하지 않는다
  }
  loadBoard();
});
setInterval(refreshQuote, 30_000);
