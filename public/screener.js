/* 스크리너: 스냅샷(전 종목 + 펀더멘털) 로드 → 장중 시세 병합 → 파생지표 계산 →
   유니버스/업종/검색 필터 → 선택한 컬럼만 표시, 컬럼 또는 사용자 수식으로 정렬.
   전부 클라이언트에서 동작한다. */
"use strict";
import { compileFormula } from "./formula.js";

const PAGE = 50;

// ---- 컬럼 정의 -----------------------------------------------------------

const fmtPrice = (v) => v === null || v === undefined ? "-" : v.toLocaleString("ko-KR");
const fmtBig = (v) => {
  if (v === null || v === undefined || !Number.isFinite(v)) return "-";
  const sign = v < 0 ? "-" : "";
  const a = Math.abs(v);
  if (a >= 1e12) return sign + (a / 1e12).toFixed(1) + "조";
  if (a >= 1e8) return sign + Math.round(a / 1e8).toLocaleString("ko-KR") + "억";
  return sign + Math.round(a).toLocaleString("ko-KR");
};
const fmtRatio = (v) => v === null || v === undefined || Number.isNaN(v)
  ? "-" : (Math.abs(v) >= 1000 ? Math.round(v).toLocaleString("ko-KR") : v.toFixed(2));
const chgClass = (v) => v === null || v === undefined ? "flat" : v > 0 ? "up" : v < 0 ? "down" : "flat";

// key: 데이터 필드 / label: 헤더 / fmt: 셀 포맷 / cls: 추가 클래스
const COLUMNS = [
  { key: "industry", label: "업종", fmt: (v) => v || "-", text: true, w: 140 },
  { key: "price", label: "현재가", fmt: fmtPrice, w: 92 },
  { key: "volume", label: "거래량", fmt: fmtBig, w: 92 },
  { key: "value", label: "거래대금", fmt: fmtBig, w: 96 },
  { key: "marketCap", label: "시가총액", fmt: fmtBig, w: 96 },
  { key: "per", label: "PER", fmt: fmtRatio, w: 78 },
  { key: "pbr", label: "PBR", fmt: fmtRatio, w: 78 },
  { key: "eps", label: "EPS", fmt: fmtPrice, w: 90 },
  { key: "bps", label: "BPS", fmt: fmtPrice, w: 90 },
  { key: "roe", label: "ROE%", fmt: fmtRatio, w: 78 },
  { key: "netIncome", label: "순이익", fmt: fmtBig, w: 96 },
  { key: "equity", label: "순자산", fmt: fmtBig, w: 96 },
  { key: "shares", label: "주식수", fmt: fmtBig, w: 104 },
  { key: "dividendYield", label: "배당률%", fmt: fmtRatio, w: 84 },
  { key: "dps", label: "주당배당금", fmt: fmtPrice, w: 100 },
  { key: "foreignRate", label: "외인%", fmt: fmtRatio, w: 78 },
  { key: "high52w", label: "52주고", fmt: fmtPrice, w: 92 },
  { key: "low52w", label: "52주저", fmt: fmtPrice, w: 92 },
  { key: "pos52", label: "52주위치%", fmt: fmtRatio, w: 94 },
];
const COL_BY_KEY = new Map(COLUMNS.map((c) => [c.key, c]));

const COLUMN_PRESETS = {
  basic: ["industry", "price", "volume", "value", "marketCap"],
  valuation: ["industry", "price", "marketCap", "per", "pbr", "roe", "eps", "bps"],
  dividend: ["industry", "price", "dividendYield", "dps", "per", "marketCap"],
  financial: ["industry", "marketCap", "netIncome", "equity", "shares", "roe", "foreignRate"],
  all: COLUMNS.map((c) => c.key),
};
const DEFAULT_COLUMNS = ["industry", "price", "value", "marketCap", "per", "pbr", "roe", "dividendYield", "foreignRate"];

// ---- 기본 제공 수식 --------------------------------------------------------

const PRESET_FORMULAS = [
  { name: "가치복합", src: "1/PER + 1/PBR" },
  { name: "이익수익률", src: "순이익 / 시총 * 100" },
  { name: "배당+가치", src: "배당률 + 100/PER" },
  { name: "순자산할인", src: "순자산 / 시총" },
  { name: "모멘텀", src: "등락률 * log(거래대금)" },
  { name: "52주위치", src: "(가격 - 저가52) / (고가52 - 저가52) * 100" },
];

// ---- 상태 ------------------------------------------------------------------

function loadJson(key, fallback) {
  try {
    const v = JSON.parse(localStorage.getItem(key) || "null");
    return v === null ? fallback : v;
  } catch (e) { return fallback; }
}
function saveJson(key, v) {
  try { localStorage.setItem(key, JSON.stringify(v)); } catch (e) {}
}

const state = {
  rows: [],
  universe: "all",           // all | KOSPI | KOSDAQ | watch
  industries: new Set(),
  search: "",
  columns: loadJson("moastock.columns", DEFAULT_COLUMNS).filter((k) => COL_BY_KEY.has(k)),
  sort: { key: "marketCap", dir: -1 },
  formula: null,             // {evaluate, src}
  savedFormulas: loadJson("moastock.formulas", []),   // [{name, src}]
  page: 1,
};
if (!state.columns.length) state.columns = [...DEFAULT_COLUMNS];

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ---- 관심종목 (대시보드와 같은 저장소 공유) ---------------------------------

let watch = new Set(loadJson("moastock.watchlist", []));
const symbolOf = (row) => row.code + (row.market === "KOSDAQ" ? ".KQ" : ".KS");
const watchHasCode = (row) => watch.has(symbolOf(row));

// ---- 파생 지표 --------------------------------------------------------------

function computeDerived(row) {
  const { price, eps, bps, marketCap, dividendYield, high52w, low52w } = row;
  row.shares = price && marketCap ? marketCap / price : null;
  row.roe = eps !== null && bps ? Math.round((eps / bps) * 1000) / 10 : null;
  row.netIncome = eps !== null && row.shares ? eps * row.shares : null;
  row.equity = bps !== null && row.shares ? bps * row.shares : null;
  row.dps = price && dividendYield !== null ? Math.round(price * dividendYield) / 100 : null;
  row.pos52 = price && high52w && low52w !== null && high52w > low52w
    ? Math.round(((price - low52w) / (high52w - low52w)) * 1000) / 10 : null;
}

// ---- 데이터 로드 -------------------------------------------------------------

async function loadSnapshot() {
  const body = await (await fetch("/data/snapshot.json")).json();
  state.rows = body.stocks.map((s) => ({ ...s }));
  state.rows.forEach(computeDerived);
  $("#data-info").textContent = `재무 기준 ${String(body.generatedAt).slice(0, 10)} · ${body.count}종목`;
  buildIndustryMenu();
  buildColumnMenu();
  renderFormulaChips();
  render();
}

async function refreshLive() {
  try {
    const results = await Promise.all(
      ["KOSPI", "KOSDAQ"].map((m) => fetch(`/api/screener?market=${m}`).then((r) => r.json())),
    );
    const live = new Map();
    for (const body of results) for (const q of body.quotes || []) live.set(q.code, q);
    for (const row of state.rows) {
      const q = live.get(row.code);
      if (!q) continue;
      for (const k of ["price", "change", "volume", "value", "marketCap"]) {
        if (q[k] !== null && q[k] !== undefined) row[k] = q[k];
      }
      if (row.eps !== null && row.price) row.per = row.eps > 0 ? Math.round((row.price / row.eps) * 100) / 100 : null;
      if (row.bps && row.price) row.pbr = Math.round((row.price / row.bps) * 100) / 100;
      computeDerived(row);
    }
    $("#data-info").textContent =
      $("#data-info").textContent.split(" · 시세")[0] +
      " · 시세 " + new Date().toLocaleTimeString("ko-KR", { hour12: false });
    render();
  } catch (e) { /* 실시간 실패 시 스냅샷 값 유지 */ }
}

// ---- 업종/컬럼 메뉴 -----------------------------------------------------------

function buildIndustryMenu() {
  const counts = new Map();
  for (const r of state.rows) {
    if (!r.industry) continue;
    counts.set(r.industry, (counts.get(r.industry) || 0) + 1);
  }
  const list = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  $("#industry-list").innerHTML = list.map(([name, n]) =>
    `<label><input type="checkbox" value="${esc(name)}"> ${esc(name)} <span class="muted">${n}</span></label>`,
  ).join("");
}

function buildColumnMenu() {
  $("#column-list").innerHTML = COLUMNS.map((c) =>
    `<label><input type="checkbox" value="${c.key}" ${state.columns.includes(c.key) ? "checked" : ""}> ${esc(c.label)}</label>`,
  ).join("");
  updateColumnCount();
}
function updateColumnCount() {
  $("#column-count").textContent = `(${state.columns.length})`;
}

// ---- 수식 칩 -------------------------------------------------------------------

function renderFormulaChips() {
  const chips = [];
  for (const f of PRESET_FORMULAS) {
    chips.push(`<button class="chip preset ${state.formula?.src === f.src ? "on" : ""}"
      data-src="${esc(f.src)}" title="${esc(f.src)}">${esc(f.name)}</button>`);
  }
  for (const [i, f] of state.savedFormulas.entries()) {
    chips.push(`<span class="chip user ${state.formula?.src === f.src ? "on" : ""}" title="${esc(f.src)}">
      <button class="chip-apply" data-src="${esc(f.src)}">${esc(f.name)}</button>
      <button class="chip-del" data-del="${i}" title="삭제">✕</button></span>`);
  }
  $("#formula-chips").innerHTML = chips.join("");
}

// ---- 필터/정렬/렌더 --------------------------------------------------------------

function filtered() {
  const q = state.search.trim().toLowerCase();
  return state.rows.filter((r) => {
    if (state.universe === "KOSPI" || state.universe === "KOSDAQ") {
      if (r.market !== state.universe) return false;
    } else if (state.universe === "watch" && !watchHasCode(r)) return false;
    if (state.industries.size && !state.industries.has(r.industry)) return false;
    if (q && !(r.name.toLowerCase().includes(q) || r.code.includes(q))) return false;
    return true;
  });
}

function sorted(rows) {
  const { key, dir } = state.sort;
  return rows.slice().sort((a, b) => {
    const av = key === "__formula" ? a.__formula : a[key];
    const bv = key === "__formula" ? b.__formula : b[key];
    const aBad = av === null || av === undefined || (typeof av === "number" && Number.isNaN(av)) || av === "";
    const bBad = bv === null || bv === undefined || (typeof bv === "number" && Number.isNaN(bv)) || bv === "";
    if (aBad && bBad) return 0;
    if (aBad) return 1;               // 값 없는 종목은 항상 뒤로
    if (bBad) return -1;
    if (typeof av === "string") return dir * av.localeCompare(bv, "ko");
    return dir * (av - bv);
  });
}

// ---- 스파크라인 (보이는 페이지만 지연 로드, /api/quote 재사용) ---------------

const sparkCache = new Map();   // code -> {spark, t}
const SPARK_TTL = 60_000;
let sparkEpoch = 0;

function sparkSvg(points, change, w = 84, h = 26) {
  if (!points || points.length < 2) return '<span class="muted">-</span>';
  const min = Math.min(...points), max = Math.max(...points);
  const span = max - min || 1;
  const coords = points
    .map((v, i) => `${((i / (points.length - 1)) * w).toFixed(1)},${(h - 2 - ((v - min) / span) * (h - 4)).toFixed(1)}`)
    .join(" ");
  const color = change > 0 ? "var(--up)" : change < 0 ? "var(--down)" : "var(--muted)";
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">` +
         `<polyline points="${coords}" style="stroke:${color}"/></svg>`;
}

async function fillSparks(rows) {
  const epoch = ++sparkEpoch;   // 페이지가 바뀌면 이전 요청 결과는 버린다
  const now = Date.now();
  const missing = rows.filter((r) => {
    const hit = sparkCache.get(r.code);
    return !hit || now - hit.t > SPARK_TTL;
  });
  for (let i = 0; i < missing.length; i += 20) {
    const part = missing.slice(i, i + 20);
    try {
      const symbols = part.map(symbolOf).join(",");
      const body = await (await fetch(`/api/quote?symbols=${encodeURIComponent(symbols)}`)).json();
      for (const q of body.quotes || []) {
        sparkCache.set(q.symbol.replace(/\.(KS|KQ)$/, ""), { spark: q.spark, t: Date.now() });
      }
      for (const sym of Object.keys(body.errors || {})) {
        sparkCache.set(sym.replace(/\.(KS|KQ)$/, ""), { spark: null, t: Date.now() });
      }
    } catch (e) { return; }
    if (epoch !== sparkEpoch) return;
    paintSparks();
  }
  if (epoch === sparkEpoch) paintSparks();
}

function paintSparks() {
  document.querySelectorAll("[data-spark]").forEach((td) => {
    const code = td.dataset.spark;
    const hit = sparkCache.get(code);
    if (!hit) return;
    const row = state.rows.find((r) => r.code === code);
    td.innerHTML = sparkSvg(hit.spark, row ? row.change : 0);
  });
}

const FIXED_W = { star: 36, name: 185, chart: 100, change: 84, formula: 94 };

function renderHead() {
  const cells = [
    `<th style="width:${FIXED_W.star}px"></th>`,
    `<th style="width:${FIXED_W.name}px" data-k="name">종목</th>`,
    `<th style="width:${FIXED_W.chart}px">차트</th>`,
    `<th class="num" style="width:${FIXED_W.change}px" data-k="change">등락률%</th>`,
  ];
  let total = FIXED_W.star + FIXED_W.name + FIXED_W.chart + FIXED_W.change;
  for (const key of state.columns) {
    const c = COL_BY_KEY.get(key);
    cells.push(`<th class="${c.text ? "" : "num"}" style="width:${c.w}px" data-k="${c.key}">${esc(c.label)}</th>`);
    total += c.w;
  }
  if (state.formula) { cells.push(`<th class="num" style="width:${FIXED_W.formula}px" data-k="__formula">수식값</th>`); total += FIXED_W.formula; }
  $("#head-row").innerHTML = cells.join("");
  // table-layout:fixed + 합산 폭 고정: 열 조합이 바뀌어도 각 열 너비는 불변
  $("#screener-table").style.width = total + "px";
  document.querySelectorAll("#head-row th[data-k]").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.k === state.sort.key);
    th.dataset.dir = th.dataset.k === state.sort.key ? (state.sort.dir > 0 ? "▲" : "▼") : "";
  });
}

function cellHtml(row, key) {
  const c = COL_BY_KEY.get(key);
  const v = row[key];
  if (c.text) return `<td class="industry-cell">${esc(c.fmt(v))}</td>`;
  const cls = c.color ? ` ${chgClass(v)}` : "";
  return `<td class="num${cls}">${c.fmt(v)}</td>`;
}

function render() {
  let rows = filtered();
  if (state.formula) {
    for (const r of rows) r.__formula = state.formula.evaluate(r);
  }
  rows = sorted(rows);

  const pages = Math.max(1, Math.ceil(rows.length / PAGE));
  state.page = Math.min(state.page, pages);
  const slice = rows.slice((state.page - 1) * PAGE, state.page * PAGE);

  renderHead();
  $("#screener-table tbody").innerHTML = slice.map((r, idx) => {
    const cached = sparkCache.get(r.code);
    const cells = [
      `<td><button class="star ${watchHasCode(r) ? "on" : ""}" data-code="${r.code}" title="관심종목">★</button></td>`,
      `<td><span class="rank muted">${(state.page - 1) * PAGE + idx + 1}</span> ${esc(r.name)}
        <div class="sym">${r.code} · ${r.market === "KOSDAQ" ? "코스닥" : "코스피"}</div></td>`,
      `<td data-spark="${r.code}">${cached ? sparkSvg(cached.spark, r.change) : '<span class="muted">·</span>'}</td>`,
      `<td class="num ${chgClass(r.change)}">${r.change === null || r.change === undefined ? "-" : r.change.toFixed(2)}</td>`,
      ...state.columns.map((key) => cellHtml(r, key)),
    ];
    if (state.formula) cells.push(`<td class="num">${fmtRatio(r.__formula ?? null)}</td>`);
    return `<tr>${cells.join("")}</tr>`;
  }).join("");
  fillSparks(slice);

  $("#row-count").textContent = `${rows.length.toLocaleString("ko-KR")}종목`;
  $("#page-info").textContent = `${state.page} / ${pages}`;
  $("#prev").disabled = state.page <= 1;
  $("#next").disabled = state.page >= pages;
}

// ---- 이벤트 -----------------------------------------------------------------------

$("#universe").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-u]");
  if (!btn) return;
  document.querySelectorAll("#universe button").forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  state.universe = btn.dataset.u;
  state.page = 1;
  render();
});

$("#industry-list").addEventListener("change", () => {
  state.industries = new Set(
    [...document.querySelectorAll("#industry-list input:checked")].map((i) => i.value),
  );
  $("#industry-count").textContent = state.industries.size ? `(${state.industries.size})` : "";
  state.page = 1;
  render();
});
$("#industry-clear").addEventListener("click", () => {
  document.querySelectorAll("#industry-list input:checked").forEach((i) => (i.checked = false));
  state.industries.clear();
  $("#industry-count").textContent = "";
  render();
});

$("#column-list").addEventListener("change", () => {
  const picked = [...document.querySelectorAll("#column-list input:checked")].map((i) => i.value);
  // 표시 순서는 COLUMNS 정의 순서를 따른다
  state.columns = COLUMNS.map((c) => c.key).filter((k) => picked.includes(k));
  if (!state.columns.length) state.columns = ["price"];
  saveJson("moastock.columns", state.columns);
  updateColumnCount();
  render();
});
$("#column-presets").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-preset]");
  if (!btn) return;
  state.columns = [...COLUMN_PRESETS[btn.dataset.preset]];
  saveJson("moastock.columns", state.columns);
  buildColumnMenu();
  render();
});

$("#search").addEventListener("input", (e) => {
  state.search = e.target.value;
  state.page = 1;
  render();
});

function applyFormulaSrc(src) {
  const errEl = $("#formula-error");
  try {
    const { evaluate } = compileFormula(src);
    state.formula = { evaluate, src };
    state.sort = { key: "__formula", dir: -1 };
    errEl.hidden = true;
    state.page = 1;
    $("#formula").value = src;
    renderFormulaChips();
    render();
  } catch (err) {
    errEl.textContent = err.message;
    errEl.hidden = false;
  }
}
function clearFormula() {
  state.formula = null;
  $("#formula-error").hidden = true;
  if (state.sort.key === "__formula") state.sort = { key: "marketCap", dir: -1 };
  renderFormulaChips();
  render();
}

$("#formula-apply").addEventListener("click", () => {
  const src = $("#formula").value.trim();
  src ? applyFormulaSrc(src) : clearFormula();
});
$("#formula").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { const s = $("#formula").value.trim(); s ? applyFormulaSrc(s) : clearFormula(); }
});
$("#formula-clear").addEventListener("click", () => { $("#formula").value = ""; clearFormula(); });

$("#formula-save").addEventListener("click", () => {
  const src = $("#formula").value.trim();
  if (!src) return;
  try { compileFormula(src); } catch (err) {
    $("#formula-error").textContent = err.message;
    $("#formula-error").hidden = false;
    return;
  }
  const name = $("#formula-name").value.trim() || `수식 ${state.savedFormulas.length + 1}`;
  const existing = state.savedFormulas.findIndex((f) => f.name === name);
  if (existing >= 0) state.savedFormulas[existing] = { name, src };
  else state.savedFormulas.push({ name, src });
  saveJson("moastock.formulas", state.savedFormulas);
  $("#formula-name").value = "";
  applyFormulaSrc(src);
});

$("#formula-chips").addEventListener("click", (e) => {
  const del = e.target.closest("[data-del]");
  if (del) {
    state.savedFormulas.splice(Number(del.dataset.del), 1);
    saveJson("moastock.formulas", state.savedFormulas);
    renderFormulaChips();
    return;
  }
  const apply = e.target.closest("[data-src]");
  if (apply) applyFormulaSrc(apply.dataset.src);
});

document.querySelector("thead").addEventListener("click", (e) => {
  const th = e.target.closest("th[data-k]");
  if (!th) return;
  const key = th.dataset.k;
  if (key === "__formula" && !state.formula) return;
  const textCol = key === "name" || COL_BY_KEY.get(key)?.text;
  state.sort = state.sort.key === key
    ? { key, dir: -state.sort.dir }
    : { key, dir: textCol ? 1 : -1 };
  render();
});

$("#screener-table").addEventListener("click", (e) => {
  const btn = e.target.closest(".star");
  if (!btn) return;
  const row = state.rows.find((r) => r.code === btn.dataset.code);
  if (!row) return;
  const sym = symbolOf(row);
  watch.has(sym) ? watch.delete(sym) : watch.add(sym);
  saveJson("moastock.watchlist", [...watch]);
  render();
});

$("#prev").addEventListener("click", () => { state.page--; render(); });
$("#next").addEventListener("click", () => { state.page++; render(); });

$("#theme-toggle").addEventListener("click", () => {
  const root = document.documentElement;
  const next = root.dataset.theme === "light" ? "dark" : "light";
  root.dataset.theme = next;
  try { localStorage.setItem("moastock.theme", next); } catch (e) {}
});
try {
  const saved = localStorage.getItem("moastock.theme");
  if (saved) document.documentElement.dataset.theme = saved;
} catch (e) {}

// ---- 시작 -------------------------------------------------------------------------

loadSnapshot().then(refreshLive).catch(() => {
  $("#data-info").textContent = "스냅샷 로드 실패";
});
setInterval(refreshLive, 60_000);
