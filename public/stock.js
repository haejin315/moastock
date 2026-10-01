/* 종목 상세: 캔들차트(분/일/주/월), 지표, 공시, 뉴스, 토론방.
   URL: /stock.html?code=005930 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const code = (new URLSearchParams(location.search).get("code") || "").trim();
if (!/^\d{6}$/.test(code)) {
  document.body.innerHTML = '<p style="padding:40px">잘못된 종목코드입니다. <a href="/screener.html">스크리너로</a></p>';
  throw new Error("bad code");
}

let stock = null;   // snapshot 행
let symbol = code + ".KS";

const fmtPrice = (v) => v === null || v === undefined ? "-" : v.toLocaleString("ko-KR");
const fmtBig = (v) => {
  if (v === null || v === undefined || !Number.isFinite(v)) return "-";
  const a = Math.abs(v), sign = v < 0 ? "-" : "";
  if (a >= 1e12) return sign + (a / 1e12).toFixed(1) + "조";
  if (a >= 1e8) return sign + Math.round(a / 1e8).toLocaleString("ko-KR") + "억";
  return sign + Math.round(a).toLocaleString("ko-KR");
};
const fmtRatio = (v) => v === null || v === undefined || Number.isNaN(v) ? "-" : v.toFixed(2);
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
    const body = await (await fetch("/data/snapshot.json")).json();
    stock = body.stocks.find((s) => s.code === code) || null;
  } catch (e) { /* 스냅샷 없이도 차트는 동작 */ }
  if (stock) {
    symbol = code + (stock.market === "KOSDAQ" ? ".KQ" : ".KS");
    document.title = `${stock.name} - 모아스톡`;
    $("#stock-title").firstChild.textContent = stock.name;
    $("#stock-code").textContent = `${code} · ${stock.market === "KOSDAQ" ? "코스닥" : "코스피"} · ${stock.industry || ""}`;
    renderFacts();
  } else {
    $("#stock-title").firstChild.textContent = code;
    $("#stock-code").textContent = code;
  }
  paintWatch();
  refreshQuote();
}

function renderFacts() {
  const s = stock;
  const shares = s.price && s.marketCap ? s.marketCap / s.price : null;
  const facts = [
    ["시가총액", fmtBig(s.marketCap)],
    ["PER", fmtRatio(s.per)], ["PBR", fmtRatio(s.pbr)],
    ["EPS", fmtPrice(s.eps)], ["BPS", fmtPrice(s.bps)],
    ["ROE", s.eps !== null && s.bps ? (s.eps / s.bps * 100).toFixed(1) + "%" : "-"],
    ["순이익(추정)", s.eps !== null && shares ? fmtBig(s.eps * shares) : "-"],
    ["순자산(추정)", s.bps !== null && shares ? fmtBig(s.bps * shares) : "-"],
    ["배당수익률", s.dividendYield !== null ? s.dividendYield.toFixed(2) + "%" : "-"],
    ["외국인 비율", s.foreignRate !== null ? s.foreignRate.toFixed(2) + "%" : "-"],
    ["52주 최고", fmtPrice(s.high52w)], ["52주 최저", fmtPrice(s.low52w)],
  ];
  $("#facts").innerHTML = facts.map(([k, v]) =>
    `<div class="fact"><div class="fact-k">${k}</div><div class="fact-v">${v}</div></div>`).join("");
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
    $("#meta").textContent = `${q.exchange} · ${q.currency} · ${q.marketState || ""}`;
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
    },
    grid: {
      vertLines: { color: css.getPropertyValue("--line").trim() || "#1e293b" },
      horzLines: { color: css.getPropertyValue("--line").trim() || "#1e293b" },
    },
    timeScale: { timeVisible: true, borderVisible: false },
    rightPriceScale: { borderVisible: false },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  });
  candleSeries = chart.addCandlestickSeries({
    upColor: "#f87171", wickUpColor: "#f87171", borderUpColor: "#f87171",     // 한국 관례: 상승=빨강
    downColor: "#60a5fa", wickDownColor: "#60a5fa", borderDownColor: "#60a5fa",
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
    chart.timeScale().fitContent();
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

async function loadBoard() {
  const el = $("#board-list");
  try {
    const body = await (await fetch(`/api/board?code=${code}`)).json();
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
        <span class="muted">${esc(String(p.created_at).replace("T", " ").slice(0, 16))} UTC</span>
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
  loadFeed("disclosure", "#disclosure-list");
  loadFeed("news", "#news-list");
  loadBoard();
});
setInterval(refreshQuote, 30_000);
