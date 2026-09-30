/* 스크리너: 스냅샷(전 종목 + 펀더멘털) 로드 → 장중 시세 병합 →
   유니버스/업종/검색 필터 → 컬럼 또는 사용자 수식으로 정렬. 전부 클라이언트에서. */
"use strict";
import { compileFormula } from "./formula.js";

const PAGE = 50;

const state = {
  rows: [],
  universe: "all",           // all | KOSPI | KOSDAQ | watch
  industries: new Set(),     // 빈 set = 전체 업종
  search: "",
  sort: { key: "marketCap", dir: -1 },
  formula: null,             // {evaluate, src}
  page: 1,
};

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ---- 관심종목 (대시보드와 같은 저장소 공유) ------------------------------

function loadWatch() {
  try { return new Set(JSON.parse(localStorage.getItem("moastock.watchlist") || "[]")); }
  catch (e) { return new Set(); }
}
function saveWatch(set) {
  try { localStorage.setItem("moastock.watchlist", JSON.stringify([...set])); } catch (e) {}
}
let watch = loadWatch();
const symbolOf = (row) => row.code + (row.market === "KOSDAQ" ? ".KQ" : ".KS");
const watchHasCode = (row) => watch.has(symbolOf(row));

// ---- 데이터 로드 ---------------------------------------------------------

async function loadSnapshot() {
  const body = await (await fetch("/data/snapshot.json")).json();
  state.rows = body.stocks.map((s) => ({
    ...s,
    roe: s.eps !== null && s.bps ? Math.round((s.eps / s.bps) * 1000) / 10 : null,
  }));
  $("#data-info").textContent = `재무 기준 ${String(body.generatedAt).slice(0, 10)} · ${body.count}종목`;
  buildIndustryMenu();
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
    }
    $("#data-info").textContent += " · 시세 " + new Date().toLocaleTimeString("ko-KR", { hour12: false });
    render();
  } catch (e) { /* 실시간 실패 시 스냅샷 값 유지 */ }
}

// ---- 업종 메뉴 -----------------------------------------------------------

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

// ---- 필터/정렬/렌더 ------------------------------------------------------

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
  const val = key === "__formula"
    ? (r) => r.__formula
    : (r) => r[key];
  return rows.slice().sort((a, b) => {
    const av = val(a), bv = val(b);
    const aBad = av === null || av === undefined || Number.isNaN(av);
    const bBad = bv === null || bv === undefined || Number.isNaN(bv);
    if (aBad && bBad) return 0;
    if (aBad) return 1;               // 값 없는 종목은 항상 뒤로
    if (bBad) return -1;
    if (typeof av === "string") return dir * av.localeCompare(bv, "ko");
    return dir * (av - bv);
  });
}

const fmt = {
  price: (v) => v === null ? "-" : v.toLocaleString("ko-KR"),
  big: (v) => {
    if (v === null || !Number.isFinite(v)) return "-";
    if (v >= 1e12) return (v / 1e12).toFixed(1) + "조";
    if (v >= 1e8) return Math.round(v / 1e8).toLocaleString("ko-KR") + "억";
    return v.toLocaleString("ko-KR");
  },
  ratio: (v) => v === null || Number.isNaN(v) ? "-" : (Math.abs(v) >= 1000 ? Math.round(v).toLocaleString("ko-KR") : v.toFixed(2)),
};
const chgClass = (v) => v === null ? "flat" : v > 0 ? "up" : v < 0 ? "down" : "flat";

function render() {
  let rows = filtered();
  if (state.formula) {
    for (const r of rows) r.__formula = state.formula.evaluate(r);
  }
  rows = sorted(rows);

  const pages = Math.max(1, Math.ceil(rows.length / PAGE));
  state.page = Math.min(state.page, pages);
  const slice = rows.slice((state.page - 1) * PAGE, state.page * PAGE);

  document.querySelectorAll(".formula-col").forEach((el) => el.hidden = !state.formula);
  $("#screener-table tbody").innerHTML = slice.map((r, idx) => `<tr>
    <td><button class="star ${watchHasCode(r) ? "on" : ""}" data-code="${r.code}" title="관심종목">★</button></td>
    <td><span class="rank muted">${(state.page - 1) * PAGE + idx + 1}</span> ${esc(r.name)}
        <div class="sym">${r.code} · ${r.market === "KOSDAQ" ? "코스닥" : "코스피"}</div></td>
    <td class="industry-cell">${esc(r.industry || "-")}</td>
    <td class="num">${fmt.price(r.price)}</td>
    <td class="num ${chgClass(r.change)}">${r.change === null ? "-" : r.change.toFixed(2)}</td>
    <td class="num">${fmt.big(r.volume)}</td>
    <td class="num">${fmt.big(r.value)}</td>
    <td class="num">${fmt.big(r.marketCap)}</td>
    <td class="num">${fmt.ratio(r.per)}</td>
    <td class="num">${fmt.ratio(r.pbr)}</td>
    <td class="num">${fmt.ratio(r.roe)}</td>
    <td class="num">${fmt.ratio(r.dividendYield)}</td>
    <td class="num">${fmt.ratio(r.foreignRate)}</td>
    <td class="num formula-col" ${state.formula ? "" : "hidden"}>${fmt.ratio(r.__formula ?? null)}</td>
  </tr>`).join("");

  $("#row-count").textContent = `${rows.length.toLocaleString("ko-KR")}종목`;
  $("#page-info").textContent = `${state.page} / ${pages}`;
  $("#prev").disabled = state.page <= 1;
  $("#next").disabled = state.page >= pages;

  document.querySelectorAll("thead th[data-k]").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.k === state.sort.key);
    th.dataset.dir = th.dataset.k === state.sort.key ? (state.sort.dir > 0 ? "▲" : "▼") : "";
  });
}

// ---- 이벤트 ---------------------------------------------------------------

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

$("#search").addEventListener("input", (e) => {
  state.search = e.target.value;
  state.page = 1;
  render();
});

function applyFormula() {
  const src = $("#formula").value.trim();
  const errEl = $("#formula-error");
  if (!src) return clearFormula();
  try {
    const { evaluate } = compileFormula(src);
    state.formula = { evaluate, src };
    state.sort = { key: "__formula", dir: -1 };
    errEl.hidden = true;
    state.page = 1;
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
  render();
}
$("#formula-apply").addEventListener("click", applyFormula);
$("#formula").addEventListener("keydown", (e) => { if (e.key === "Enter") applyFormula(); });
$("#formula-clear").addEventListener("click", () => { $("#formula").value = ""; clearFormula(); });

document.querySelector("thead").addEventListener("click", (e) => {
  const th = e.target.closest("th[data-k]");
  if (!th) return;
  const key = th.dataset.k;
  if (key === "__formula" && !state.formula) return;
  state.sort = state.sort.key === key
    ? { key, dir: -state.sort.dir }
    : { key, dir: key === "name" || key === "industry" ? 1 : -1 };
  render();
});

$("#screener-table").addEventListener("click", (e) => {
  const btn = e.target.closest(".star");
  if (!btn) return;
  const row = state.rows.find((r) => r.code === btn.dataset.code);
  if (!row) return;
  const sym = symbolOf(row);
  watch.has(sym) ? watch.delete(sym) : watch.add(sym);
  saveWatch(watch);
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

// ---- 시작 -----------------------------------------------------------------

loadSnapshot().then(refreshLive).catch(() => {
  $("#data-info").textContent = "스냅샷 로드 실패";
});
setInterval(refreshLive, 90_000);
