/* 모아스톡 대시보드: /api/* (Cloudflare Pages Functions 프록시)만 호출한다. */
"use strict";

// [야후 기호, 이름, 시장] - 시장 탭(주요·국내·미국·일본·코인·원자재)으로 골라 본다
const INDICES = [
  ["^KS11", "코스피", "KR"], ["^KQ11", "코스닥", "KR"], ["KRW=X", "원/달러", "KR"],
  ["^GSPC", "S&P 500", "US"], ["^IXIC", "나스닥", "US"], ["^DJI", "다우존스", "US"], ["^NDX", "나스닥 100", "US"],
  ["^SOX", "필라델피아 반도체", "US"],
  ["^N225", "닛케이 225", "JP"], ["JPYKRW=X", "엔/원", "JP"],
  ["BTC-USD", "비트코인", "COIN"], ["ETH-USD", "이더리움", "COIN"], ["XRP-USD", "리플", "COIN"], ["SOL-USD", "솔라나", "COIN"],
  ["CL=F", "WTI 유가", "ETC"], ["GC=F", "금", "ETC"],
];
const HEADLINE = ["^KS11", "^KQ11", "^GSPC", "^IXIC", "^N225", "KRW=X", "BTC-USD", "CL=F"];
const TICKER_TABS = [["MAIN", "주요"], ["KR", "국내"], ["US", "미국"], ["JP", "일본"], ["COIN", "코인"], ["ETC", "원자재"]];
let tickerTab = "MAIN";
try { tickerTab = localStorage.getItem("moastock.tickerTab") || "MAIN"; } catch (e) {}
const shownIndices = () => INDICES.filter(([sym, , m]) => tickerTab === "MAIN" ? HEADLINE.includes(sym) : m === tickerTab);
const KNOWN_NAMES = new Map([
  ...INDICES.map(([sym, name]) => [sym, name]),
  ["005930.KS", "삼성전자"],
  ["000660.KS", "SK하이닉스"],
  ["373220.KS", "LG에너지솔루션"],
  ["035420.KS", "NAVER"],
  ["AAPL", "애플"],
  ["NVDA", "엔비디아"],
  ["MSFT", "마이크로소프트"],
  ["TSLA", "테슬라"],
]);
const DEFAULT_WATCHLIST = ["005930.KS", "000660.KS", "035420.KS", "AAPL", "NVDA", "TSLA"];
const REFRESH_MS = 60_000;

// ---- 상태 ---------------------------------------------------------------

function loadWatchlist() {
  try {
    const raw = JSON.parse(localStorage.getItem("moastock.watchlist") || "null");
    if (Array.isArray(raw) && raw.length) return raw;
  } catch (e) { /* 저장소 접근 불가 - 기본값 사용 */ }
  return [...DEFAULT_WATCHLIST];
}
function saveWatchlist(list) {
  try { localStorage.setItem("moastock.watchlist", JSON.stringify(list)); } catch (e) {}
}
let watchlist = loadWatchlist();

// ---- 표시 유틸 -----------------------------------------------------------

function fmtPrice(value, currency) {
  if (value === null || value === undefined) return "-";
  const digits = currency === "KRW" || value >= 10000 ? 0 : 2;
  return value.toLocaleString("ko-KR", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}
function chgClass(pct) {
  if (pct === null || pct === undefined || Math.abs(pct) < 0.005) return "flat";
  return pct > 0 ? "up" : "down";
}
function fmtChg(pct) {
  if (pct === null || pct === undefined) return "-";
  const arrow = pct > 0 ? "▲" : pct < 0 ? "▼" : "";
  return `${arrow} ${Math.abs(pct).toFixed(2)}%`;
}
function sparkSvg(points, pct, w = 90, h = 28) {
  if (!points || points.length < 2) return "";
  const min = Math.min(...points), max = Math.max(...points);
  const span = max - min || 1;
  const coords = points
    .map((v, i) => `${((i / (points.length - 1)) * w).toFixed(1)},${(h - 2 - ((v - min) / span) * (h - 4)).toFixed(1)}`)
    .join(" ");
  const color = pct > 0 ? "var(--up)" : pct < 0 ? "var(--down)" : "var(--muted)";
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">` +
         `<polyline points="${coords}" style="stroke:${color}"/></svg>`;
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---- 시세 ---------------------------------------------------------------

async function fetchQuotes(symbols) {
  const resp = await fetch(`/api/quote?symbols=${encodeURIComponent(symbols.join(","))}`);
  if (!resp.ok) throw new Error(`quote API ${resp.status}`);
  return resp.json();
}

function renderTickers(bySymbol) {
  const strip = document.getElementById("ticker-strip");
  strip.innerHTML = shownIndices().map(([sym, name]) => {
    const q = bySymbol.get(sym);
    if (!q) return `<div class="ticker"><div class="t-name">${esc(name)}</div><div class="muted">-</div></div>`;
    return `<div class="ticker">
      <div class="t-name">${esc(name)}</div>
      <div class="t-price">${fmtPrice(q.price, q.currency)}</div>
      <div class="t-chg ${chgClass(q.changePct)}">${fmtChg(q.changePct)}</div>
    </div>`;
  }).join("");
}

// 관심종목 기호 → 종목 상세 링크 (지수·환율·선물은 링크 없음)
function stockLink(sym, label) {
  const m = sym.match(/^(\d{6})\.(KS|KQ)$/);
  if (m) return `<a href="/stock.html?code=${m[1]}">${label}</a>`;
  const market = /\.T$/.test(sym) ? "JP" : /-KRW$/.test(sym) ? "COIN" : /^[A-Z][A-Z.-]{0,9}$/.test(sym) ? "US" : null;
  return market ? `<a href="/stock.html?market=${market}&symbol=${encodeURIComponent(sym)}">${label}</a>` : label;
}

function renderWatchlist(bySymbol, errors) {
  const tbody = document.querySelector("#watchlist tbody");
  tbody.innerHTML = watchlist.map((sym) => {
    const q = bySymbol.get(sym);
    const name = stockLink(sym, esc(KNOWN_NAMES.get(sym) || sym));
    if (!q) {
      const why = errors && errors[sym] ? "조회 실패" : "…";
      return `<tr><td>${name}<div class="sym">${esc(sym)}</div></td>
        <td class="num muted">${why}</td><td></td><td></td>
        <td><button class="rm" data-sym="${esc(sym)}" title="삭제">✕</button></td></tr>`;
    }
    return `<tr>
      <td>${name}<div class="sym">${esc(sym)}</div></td>
      <td class="num">${fmtPrice(q.price, q.currency)}</td>
      <td class="num ${chgClass(q.changePct)}">${fmtChg(q.changePct)}</td>
      <td>${sparkSvg(q.spark, q.changePct)}</td>
      <td><button class="rm" data-sym="${esc(sym)}" title="삭제">✕</button></td>
    </tr>`;
  }).join("");
}

async function refreshQuotes() {
  const symbols = [...new Set([...shownIndices().map(([s]) => s), ...watchlist])].slice(0, 24);
  try {
    const { quotes, errors } = await fetchQuotes(symbols);
    const bySymbol = new Map(quotes.map((q) => [q.symbol, q]));
    renderTickers(bySymbol);
    renderWatchlist(bySymbol, errors);
    document.getElementById("updated-at").textContent =
      "업데이트 " + new Date().toLocaleTimeString("ko-KR", { hour12: false });
  } catch (err) {
    document.getElementById("updated-at").textContent = "시세 조회 실패";
  }
}

function renderTickerTabs() {
  const el = document.getElementById("ticker-tabs");
  el.innerHTML = TICKER_TABS.map(([v, label]) =>
    `<button role="tab" data-r="${v}" class="${v === tickerTab ? "active" : ""}" aria-selected="${v === tickerTab}">${label}</button>`,
  ).join("");
  const more = { KR: "KR", US: "US", JP: "JP", COIN: "COIN" }[tickerTab];
  document.getElementById("screener-link").href = more ? `/screener.html?market=${more}` : "/screener.html";
}
document.getElementById("ticker-tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-r]");
  if (!btn) return;
  tickerTab = btn.dataset.r;
  try { localStorage.setItem("moastock.tickerTab", tickerTab); } catch (err) {}
  renderTickerTabs();
  document.getElementById("ticker-strip").innerHTML = '<span class="muted">불러오는 중…</span>';
  refreshQuotes();
});
renderTickerTabs();

// ---- 공시 ---------------------------------------------------------------

async function loadDart() {
  const el = document.getElementById("dart-list");
  try {
    const body = await (await fetch("/api/dart")).json();
    if (body.error === "key_missing") {
      el.innerHTML = `<li class="muted">${esc(body.message)}</li>`;
      return;
    }
    if (!body.items || !body.items.length) {
      // 원본 오류 문자열은 사용자에게 보이지 않게 (원인은 /api/dart 응답에 남아 있다)
      el.innerHTML = body.error
        ? '<li class="muted">공시를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.</li>'
        : '<li class="muted">최근 7일 공시가 없습니다</li>';
      return;
    }
    el.innerHTML = body.items.map((d) => `<li>
      <a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.title)}</a>
      <div class="meta"><span class="badge">${esc(d.market)}</span>
        <span>${esc(d.corp)}</span><span>${esc(d.date)}</span></div>
    </li>`).join("");
  } catch (err) {
    el.innerHTML = `<li class="muted">공시 조회 실패</li>`;
  }
}

// ---- 뉴스 ---------------------------------------------------------------

async function loadNews(src) {
  const el = document.getElementById("news-list");
  el.innerHTML = `<li class="muted">불러오는 중…</li>`;
  try {
    const body = await (await fetch(`/api/news?src=${encodeURIComponent(src)}`)).json();
    if (!body.items.length) {
      el.innerHTML = `<li class="muted">기사를 가져오지 못했습니다${body.error ? " (" + esc(body.error) + ")" : ""}</li>`;
      return;
    }
    el.innerHTML = body.items.map((n) => {
      let when = "";
      const t = Date.parse(n.publishedAt);
      if (!Number.isNaN(t)) {
        const mins = Math.max(0, Math.round((Date.now() - t) / 60000));
        when = mins < 60 ? `${mins}분 전`
             : mins < 1440 ? `${Math.round(mins / 60)}시간 전`
             : new Date(t).toLocaleDateString("ko-KR");
      }
      return `<li><a href="${esc(n.link)}" target="_blank" rel="noopener">${esc(n.title)}</a>
        <div class="meta"><span>${esc(body.source)}</span><span>${when}</span></div></li>`;
    }).join("");
  } catch (err) {
    el.innerHTML = `<li class="muted">뉴스 조회 실패</li>`;
  }
}

// ---- 이벤트 -------------------------------------------------------------

document.getElementById("add-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = document.getElementById("add-input");
  const sym = input.value.trim().toUpperCase();
  if (!sym || watchlist.includes(sym)) return;
  watchlist.push(sym);
  saveWatchlist(watchlist);
  input.value = "";
  refreshQuotes();
});

document.querySelector("#watchlist tbody").addEventListener("click", (e) => {
  const btn = e.target.closest(".rm");
  if (!btn) return;
  watchlist = watchlist.filter((s) => s !== btn.dataset.sym);
  saveWatchlist(watchlist);
  refreshQuotes();
});

document.getElementById("news-tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-src]");
  if (!btn) return;
  document.querySelectorAll("#news-tabs button").forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  loadNews(btn.dataset.src);
});

document.getElementById("theme-toggle").addEventListener("click", () => {
  const root = document.documentElement;
  const next = root.dataset.theme === "light" ? "dark" : "light";
  root.dataset.theme = next;
  try { localStorage.setItem("moastock.theme", next); } catch (e) {}
});
try {
  const saved = localStorage.getItem("moastock.theme");
  if (saved) document.documentElement.dataset.theme = saved;
} catch (e) {}

// ---- 시작 ---------------------------------------------------------------

refreshQuotes();
loadDart();
loadNews("hk");
setInterval(refreshQuotes, REFRESH_MS);
