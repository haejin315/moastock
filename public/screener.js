/* 스크리너: 스냅샷(전 종목 + 펀더멘털) 로드 → 장중 시세 병합 → 파생지표 계산 →
   유니버스/업종/검색 필터 → 선택한 컬럼만 표시, 컬럼 또는 사용자 수식으로 정렬.
   수식은 여러 개를 동시에 켤 수 있고(각각 컬럼으로 추가), 켜고 끄는 것은
   정렬을 바꾸지 않는다 - 그 수식 컬럼의 헤더를 눌렀을 때만 정렬이 바뀐다.
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

// key: 데이터 필드 / label: 헤더 / fmt: 셀 포맷 / w: 고정 폭(px)
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
  sorts: [],                // 다중 정렬 [{key, dir}] - 0번이 최우선 (최근 선택)
  formulas: [],              // 활성 수식 컬럼: [{id, name, src, evaluate}]
  savedFormulas: loadJson("moastock.formulas", []),   // [{name, src}]
  page: 1,
};
if (!state.columns.length) state.columns = [...DEFAULT_COLUMNS];
let nextFormulaId = 1;
const formulaKey = (f) => `__f${f.id}`;

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

// ---- 수식 활성화 (토글: 정렬은 건드리지 않는다) ------------------------------

function activateFormula(src, name) {
  const { evaluate } = compileFormula(src);   // 실패 시 throw - 호출부에서 처리
  const f = { id: nextFormulaId++, name: name || src, src, evaluate };
  state.formulas.push(f);
  return f;
}

function toggleFormula(src, name) {
  const idx = state.formulas.findIndex((f) => f.src === src);
  if (idx >= 0) {
    const key = formulaKey(state.formulas[idx]);
    state.formulas.splice(idx, 1);
    removeSort(key);
  } else {
    activateFormula(src, name);
  }
  saveJson("moastock.activeFormulas", state.formulas.map((f) => ({ name: f.name, src: f.src })));
  renderFormulaChips();
  render();
}

function restoreActiveFormulas() {
  for (const f of loadJson("moastock.activeFormulas", [])) {
    try { activateFormula(f.src, f.name); } catch (e) { /* 깨진 저장분은 무시 */ }
  }
}

// ---- 데이터 로드 -------------------------------------------------------------

async function loadSnapshot() {
  const body = await (await fetch("/data/snapshot.json")).json();
  state.rows = body.stocks.map((s) => ({ ...s }));
  state.rows.forEach(computeDerived);
  $("#data-info").textContent = `재무 기준 ${String(body.generatedAt).slice(0, 10)} · ${body.count}종목`;
  restoreActiveFormulas();
  restoreSorts();
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

const isActiveSrc = (src) => state.formulas.some((f) => f.src === src);

function renderFormulaChips() {
  const chips = [];
  for (const f of PRESET_FORMULAS) {
    chips.push(`<button class="chip preset ${isActiveSrc(f.src) ? "on" : ""}"
      data-src="${esc(f.src)}" data-name="${esc(f.name)}" title="${esc(f.src)}">${esc(f.name)}</button>`);
  }
  for (const [i, f] of state.savedFormulas.entries()) {
    chips.push(`<span class="chip user ${isActiveSrc(f.src) ? "on" : ""}" title="${esc(f.src)}">
      <button class="chip-apply" data-src="${esc(f.src)}" data-name="${esc(f.name)}">${esc(f.name)}</button>
      <button class="chip-del" data-del="${i}" title="저장 삭제">✕</button></span>`);
  }
  $("#formula-chips").innerHTML = chips.join("");
}

// ---- 필터/정렬 --------------------------------------------------------------

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

const isBad = (v) => v === null || v === undefined || v === "" || (typeof v === "number" && Number.isNaN(v));

function compareBy(a, b, key, dir) {
  const av = a[key], bv = b[key];
  const aBad = isBad(av), bBad = isBad(bv);
  if (aBad && bBad) return 0;
  if (aBad) return 1;               // 값 없는 종목은 방향과 무관하게 항상 뒤로
  if (bBad) return -1;
  if (typeof av === "string") return dir * av.localeCompare(bv, "ko");
  return dir * (av - bv);
}

function sorted(rows) {
  const keys = state.sorts.length ? state.sorts : [{ key: "marketCap", dir: -1 }];
  return rows.slice().sort((a, b) => {
    for (const { key, dir } of keys) {   // 우선순위 순으로, 동률이면 다음 기준
      const c = compareBy(a, b, key, dir);
      if (c) return c;
    }
    return 0;
  });
}

// ---- 다중 정렬 관리 ------------------------------------------------------------

const defaultDir = (key) => (key === "name" || COL_BY_KEY.get(key)?.text ? 1 : -1);

function sortLabel(key) {
  if (key === "name") return "종목";
  if (key === "change") return "등락률%";
  if (key.startsWith("__f")) return state.formulas.find((f) => formulaKey(f) === key)?.name || "수식";
  return COL_BY_KEY.get(key)?.label || key;
}

// 저장 시 수식 키(__f{id})는 세션마다 바뀌므로 수식 원문으로 바꿔 둔다
function saveSorts() {
  saveJson("moastock.sorts", state.sorts.map(({ key, dir }) => {
    if (!key.startsWith("__f")) return { key, dir };
    const f = state.formulas.find((x) => formulaKey(x) === key);
    return f ? { src: f.src, dir } : null;
  }).filter(Boolean));
}
function restoreSorts() {
  const known = (k) => k === "name" || k === "change" || COL_BY_KEY.has(k);
  state.sorts = loadJson("moastock.sorts", [{ key: "marketCap", dir: -1 }]).map((s) => {
    if (s.src) {
      const f = state.formulas.find((x) => x.src === s.src);
      return f ? { key: formulaKey(f), dir: s.dir } : null;
    }
    return known(s.key) ? { key: s.key, dir: s.dir } : null;
  }).filter(Boolean);
}

// 헤더 클릭: 새 기준이면 맨 앞에 추가, 이미 있으면 맨 앞으로 올림,
// 이미 맨 앞이면 방향만 뒤집는다
function pickSort(key) {
  const idx = state.sorts.findIndex((s) => s.key === key);
  if (idx === 0) {
    state.sorts[0].dir = -state.sorts[0].dir;
  } else if (idx > 0) {
    const [item] = state.sorts.splice(idx, 1);
    state.sorts.unshift(item);
  } else {
    state.sorts.unshift({ key, dir: defaultDir(key) });
  }
  saveSorts();
}

function removeSort(key) {
  state.sorts = state.sorts.filter((s) => s.key !== key);
  saveSorts();
}

function renderSortChips() {
  const el = $("#sort-chips");
  if (!state.sorts.length) {
    el.innerHTML = '<span class="muted">정렬 기준 없음 — 컬럼 제목을 누르면 추가됩니다 (기본: 시가총액순)</span>';
    return;
  }
  el.innerHTML = '<span class="muted sort-title">정렬</span>' + state.sorts.map((s, i) => `
    <span class="chip sort-chip ${i === 0 ? "on" : ""}" draggable="true" data-i="${i}" title="드래그로 순서 변경">
      <span class="sort-rank">${i + 1}</span>
      ${esc(sortLabel(s.key))}
      <button class="sort-dir" data-i="${i}" title="오름/내림 전환">${s.dir > 0 ? "▲" : "▼"}</button>
      <button class="chip-del" data-rm="${i}" title="정렬 기준 삭제">✕</button>
    </span>`).join("");
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

// ---- 테이블 렌더 ----------------------------------------------------------------

const FIXED_W = { star: 36, name: 185, chart: 100, change: 84, formula: 110 };

function renderHead() {
  const cells = [
    `<th style="width:${FIXED_W.star}px"></th>`,
    `<th data-k="name">종목</th>`,
    `<th style="width:${FIXED_W.chart}px">차트</th>`,
    `<th class="num" style="width:${FIXED_W.change}px" data-k="change">등락률%</th>`,
  ];
  let total = FIXED_W.star + FIXED_W.name + FIXED_W.chart + FIXED_W.change;
  for (const key of state.columns) {
    const c = COL_BY_KEY.get(key);
    cells.push(`<th class="${c.text ? "" : "num"}" style="width:${c.w}px" data-k="${c.key}">${esc(c.label)}</th>`);
    total += c.w;
  }
  for (const f of state.formulas) {
    cells.push(`<th class="num formula-head" style="width:${FIXED_W.formula}px"
      data-k="${formulaKey(f)}" title="${esc(f.src)} — 클릭하면 이 수식으로 정렬">${esc(f.name)}</th>`);
    total += FIXED_W.formula;
  }
  $("#head-row").innerHTML = cells.join("");
  // table-layout:fixed - 숫자 열은 px 고정, 폭 미지정인 '종목' 열이 남는 공간을
  // 흡수해 테이블이 항상 페이지 폭을 채운다. 합계가 화면보다 크면 가로 스크롤.
  const tbl = $("#screener-table");
  tbl.style.width = "100%";
  tbl.style.minWidth = total + "px";
  document.querySelectorAll("#head-row th[data-k]").forEach((th) => {
    const i = state.sorts.findIndex((s) => s.key === th.dataset.k);
    th.classList.toggle("sorted", i >= 0);
    th.classList.toggle("sorted-primary", i === 0);
    th.dataset.dir = i >= 0 ? `${state.sorts[i].dir > 0 ? "▲" : "▼"}${state.sorts.length > 1 ? i + 1 : ""}` : "";
  });
}

function cellHtml(row, key) {
  const c = COL_BY_KEY.get(key);
  const v = row[key];
  if (c.text) return `<td class="industry-cell">${esc(c.fmt(v))}</td>`;
  return `<td class="num">${c.fmt(v)}</td>`;
}

function render() {
  let rows = filtered();
  for (const f of state.formulas) {
    const key = formulaKey(f);
    for (const r of rows) r[key] = f.evaluate(r);
  }
  // 정렬 키가 꺼진 수식을 가리키면 기본 정렬로
  const liveKeys = new Set(state.formulas.map(formulaKey));
  state.sorts = state.sorts.filter((s) => !s.key.startsWith("__f") || liveKeys.has(s.key));
  renderSortChips();
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
      ...state.formulas.map((f) => `<td class="num">${fmtRatio(r[formulaKey(f)] ?? null)}</td>`),
    ];
    return `<tr class="rowlink" data-code="${r.code}">${cells.join("")}</tr>`;
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

// ---- 수식 이벤트: 적용/칩 = 컬럼 토글 (정렬 유지), 저장 = 이름 붙여 보관 --------

function showFormulaError(err) {
  $("#formula-error").textContent = err.message;
  $("#formula-error").hidden = false;
}

function tryToggle(src, name) {
  try {
    toggleFormula(src, name);
    $("#formula-error").hidden = true;
  } catch (err) { showFormulaError(err); }
}

$("#formula-apply").addEventListener("click", () => {
  const src = $("#formula").value.trim();
  if (!src) return;
  tryToggle(src, $("#formula-name").value.trim() || src);
});
$("#formula").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  const src = $("#formula").value.trim();
  if (src) tryToggle(src, $("#formula-name").value.trim() || src);
});
$("#formula-clear").addEventListener("click", () => {
  state.formulas = [];
  saveJson("moastock.activeFormulas", []);
  state.sorts = state.sorts.filter((s) => !s.key.startsWith("__f"));
  saveSorts();
  $("#formula-error").hidden = true;
  renderFormulaChips();
  render();
});

$("#formula-save").addEventListener("click", () => {
  const src = $("#formula").value.trim();
  if (!src) return;
  try { compileFormula(src); } catch (err) { showFormulaError(err); return; }
  const name = $("#formula-name").value.trim() || `수식 ${state.savedFormulas.length + 1}`;
  const existing = state.savedFormulas.findIndex((f) => f.name === name);
  if (existing >= 0) state.savedFormulas[existing] = { name, src };
  else state.savedFormulas.push({ name, src });
  saveJson("moastock.formulas", state.savedFormulas);
  $("#formula-name").value = "";
  if (!isActiveSrc(src)) tryToggle(src, name);   // 저장과 동시에 컬럼으로 켠다 (정렬은 유지)
  else renderFormulaChips();
});

$("#formula-chips").addEventListener("click", (e) => {
  const del = e.target.closest("[data-del]");
  if (del) {
    const removed = state.savedFormulas.splice(Number(del.dataset.del), 1)[0];
    saveJson("moastock.formulas", state.savedFormulas);
    if (removed && isActiveSrc(removed.src)) toggleFormula(removed.src);   // 활성 컬럼도 끈다
    else renderFormulaChips();
    return;
  }
  const apply = e.target.closest("[data-src]");
  if (apply) tryToggle(apply.dataset.src, apply.dataset.name);
});

document.querySelector("thead").addEventListener("click", (e) => {
  const th = e.target.closest("th[data-k]");
  if (!th) return;
  pickSort(th.dataset.k);
  state.page = 1;
  render();
});

// 정렬 칩: 방향 전환 / 삭제 / 드래그로 우선순위 변경
$("#sort-chips").addEventListener("click", (e) => {
  const dirBtn = e.target.closest(".sort-dir");
  if (dirBtn) {
    const s = state.sorts[Number(dirBtn.dataset.i)];
    s.dir = -s.dir;
  } else {
    const rm = e.target.closest("[data-rm]");
    if (!rm) return;
    state.sorts.splice(Number(rm.dataset.rm), 1);
  }
  saveSorts();
  state.page = 1;
  render();
});

let dragFrom = null;
$("#sort-chips").addEventListener("dragstart", (e) => {
  const chip = e.target.closest(".sort-chip");
  if (!chip) return;
  dragFrom = Number(chip.dataset.i);
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData("text/plain", String(dragFrom));
  chip.classList.add("dragging");
});
$("#sort-chips").addEventListener("dragover", (e) => {
  const chip = e.target.closest(".sort-chip");
  if (!chip || dragFrom === null) return;
  e.preventDefault();
  document.querySelectorAll(".sort-chip.drop-target").forEach((c) => c.classList.remove("drop-target"));
  chip.classList.add("drop-target");
});
$("#sort-chips").addEventListener("drop", (e) => {
  const chip = e.target.closest(".sort-chip");
  if (!chip || dragFrom === null) return;
  e.preventDefault();
  const to = Number(chip.dataset.i);
  if (to !== dragFrom) {
    const [item] = state.sorts.splice(dragFrom, 1);
    state.sorts.splice(to, 0, item);
    saveSorts();
    state.page = 1;
  }
  dragFrom = null;
  render();
});
$("#sort-chips").addEventListener("dragend", () => {
  dragFrom = null;
  document.querySelectorAll(".sort-chip").forEach((c) => c.classList.remove("dragging", "drop-target"));
});

$("#screener-table").addEventListener("click", (e) => {
  const btn = e.target.closest(".star");
  if (btn) {
    const row = state.rows.find((r) => r.code === btn.dataset.code);
    if (!row) return;
    const sym = symbolOf(row);
    watch.has(sym) ? watch.delete(sym) : watch.add(sym);
    saveJson("moastock.watchlist", [...watch]);
    render();
    return;
  }
  const tr = e.target.closest("tr.rowlink");
  if (tr) location.href = `/stock.html?code=${tr.dataset.code}`;
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
