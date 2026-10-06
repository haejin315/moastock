/* 오른쪽 아래 채팅 버튼 + 메신저 창.
   - 채팅: 실시간 채팅방 (방 목록 / 방 만들기 / 대화 / 방장 설정)
   - AI 비서: 준비 중
   서버: /api/chat/* (Durable Objects WebSocket). 사용자 입력은 전부 textContent로만 그린다(XSS 방지). */

const LS = {
  get(k, d = null) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* 저장 불가 환경 */ } },
  del(k) { try { localStorage.removeItem(k); } catch { /* 무시 */ } },
};
const CLIENT_KEY = "moastock.chat.client";
const NICK_KEY = "moastock.chat.nick";
const ownerKey = (id) => `moastock.chat.owner.${id}`;

function clientId() {
  let id = LS.get(CLIENT_KEY);
  if (!id) { id = crypto.randomUUID(); LS.set(CLIENT_KEY, id); }
  return id;
}

// 작은 DOM 도우미: h("div.cls", {attrs}, ...children) - 문자열 자식은 텍스트 노드로
function h(tag, attrs = {}, ...kids) {
  // 속성 없이 자식부터 넘긴 경우 h("div", 자식, …)
  if (attrs instanceof Node || typeof attrs === "string" || Array.isArray(attrs) || attrs === null) {
    kids.unshift(attrs); attrs = {};
  }
  const [name, ...cls] = tag.split(".");
  const el = document.createElement(name);
  if (cls.length) el.className = cls.join(" ");
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c !== null && c !== undefined && c !== false) el.append(c instanceof Node ? c : String(c));
  return el;
}

const fmtTime = (ts) => new Date(ts).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" });

const state = {
  open: false, tab: "chat", view: "list",     // list | create | room | settings | password | nick
  rooms: [], limits: null, listTimer: null,
  room: null, me: null, members: [], ws: null, joinTarget: null, retry: 0, leaving: false,
  unread: 0,
};

// ---- 뼈대 -----------------------------------------------------------------------
const fab = h("button.chat-fab", { type: "button", "aria-label": "채팅 열기", title: "채팅" },
  h("span.chat-fab-icon", { "aria-hidden": "true", text: "💬" }), h("span.chat-badge", { hidden: true }));
const panel = h("section.chat-panel", { "aria-label": "채팅", hidden: true });
document.body.append(fab, panel);

fab.addEventListener("click", () => setOpen(!state.open));
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && state.open) setOpen(false); });

function setOpen(open) {
  state.open = open;
  panel.hidden = !open;
  fab.classList.toggle("on", open);
  fab.setAttribute("aria-label", open ? "채팅 닫기" : "채팅 열기");
  document.documentElement.classList.toggle("chat-open", open);
  if (open) { state.unread = 0; paintBadge(); render(); if (state.view === "list") loadRooms(); }
  else stopListTimer();
}

function paintBadge() {
  const b = fab.querySelector(".chat-badge");
  b.hidden = !state.unread;
  b.textContent = state.unread > 99 ? "99+" : String(state.unread);
}

function go(view) {
  state.view = view;
  if (view === "list") loadRooms(); else stopListTimer();
  render();
}

// ---- 서버 -----------------------------------------------------------------------
async function api(path, opts) {
  const r = await fetch(`/api/chat${path}`, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `요청 실패 (${r.status})`);
  return body;
}

async function loadRooms() {
  stopListTimer();
  try {
    const b = await api("/rooms");
    state.rooms = b.rooms; state.limits = b.limits; state.listError = null;
  } catch (e) { state.listError = e.message; }
  // 목록이 실제로 보일 때만 다시 그린다 (닉네임 입력 중에 다시 그리면 입력이 지워진다)
  const listShown = () => state.open && state.tab === "chat" && state.view === "list" && LS.get(NICK_KEY);
  if (listShown()) render();
  if (listShown()) state.listTimer = setTimeout(loadRooms, 10000);
}
function stopListTimer() { clearTimeout(state.listTimer); state.listTimer = null; }

function connect(room, password) {
  disconnect();
  state.leaving = false;
  state.joinTarget = { room, password };
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/chat/rooms/${room.id}/ws`);
  state.ws = ws;
  state.messages = [];
  state.room = room; state.me = null; state.members = [];
  state.view = "room"; state.status = "연결 중…";
  render();
  ws.addEventListener("open", () => {
    ws.send(JSON.stringify({ t: "join", nick: LS.get(NICK_KEY), client: clientId(), password,
                             owner: LS.get(ownerKey(room.id)) || undefined }));
  });
  ws.addEventListener("message", (e) => onServer(JSON.parse(e.data)));
  ws.addEventListener("close", (e) => {
    if (state.ws !== ws) return;
    state.ws = null;
    clearInterval(state.pingTimer);
    if (state.leaving) return;
    if ([4403, 4429, 4400, 4404, 4410].includes(e.code)) return;    // 거절·삭제: 다시 붙지 않는다
    if (state.retry < 5) {                                           // 끊김: 잠시 뒤 다시 입장
      state.retry++;
      state.status = `연결이 끊겼습니다. 다시 연결하는 중… (${state.retry}/5)`;
      render();
      setTimeout(() => { if (state.view === "room" && !state.leaving) connect(room, password); }, 1500 * state.retry);
    } else { state.status = "연결할 수 없습니다. 목록으로 돌아가 다시 시도해 주세요"; render(); }
  });
  clearInterval(state.pingTimer);
  state.pingTimer = setInterval(() => { if (ws.readyState === 1) ws.send('{"t":"ping"}'); }, 25000);
}

function disconnect() {
  state.leaving = true;
  clearInterval(state.pingTimer);
  if (state.ws) { try { state.ws.close(1000, "leave"); } catch { /* 무시 */ } }
  state.ws = null;
}

function send(obj) {
  if (state.ws?.readyState === 1) state.ws.send(JSON.stringify(obj));
}

function onServer(m) {
  if (m.t === "pong") return;
  if (m.t === "welcome") {
    state.retry = 0; state.status = null;
    state.room = m.room; state.me = m.me; state.members = m.members;
    state.messages = m.history.map((x) => ({ ...x, kind: "msg" }));
    state.messages.push({ kind: "sys", text: `${m.room.title}에 들어왔습니다` });
    return render();
  }
  if (m.t === "msg") {
    state.messages.push({ ...m, kind: "msg" });
    if (!state.open || state.view !== "room") { state.unread++; paintBadge(); }
    return appendMessage({ ...m, kind: "msg" });
  }
  if (m.t === "sys") {
    state.members = m.members || state.members;
    state.messages.push({ kind: "sys", text: m.text });
    appendMessage({ kind: "sys", text: m.text });
    return paintRoomHead();
  }
  if (m.t === "meta") { state.room = { ...state.room, ...m.room }; return paintRoomHead(); }
  if (m.t === "closed") {
    state.leaving = true;
    LS.del(ownerKey(state.room?.id));
    state.notice = m.msg;
    return go("list");
  }
  if (m.t === "error") {
    if (m.code === "password") { state.passwordError = m.msg; state.view = "password"; return render(); }
    if (m.code === "nick") { state.nickError = m.msg; state.view = "nick"; return render(); }
    if (m.code === "full") { state.notice = m.msg; return go("list"); }
    state.toast = m.msg; return paintToast();
  }
}

// ---- 화면 -----------------------------------------------------------------------
function render() {
  if (!state.open) return;
  const tabs = h("div.chat-tabs", { role: "tablist" },
    h("button", { type: "button", role: "tab", "aria-selected": String(state.tab === "chat"),
                  class: state.tab === "chat" ? "on" : "", onclick: () => { state.tab = "chat"; render(); } }, "채팅"),
    h("button", { type: "button", role: "tab", "aria-selected": String(state.tab === "ai"),
                  class: state.tab === "ai" ? "on" : "", onclick: () => { state.tab = "ai"; render(); } }, "AI 비서"),
    h("button.chat-close", { type: "button", "aria-label": "채팅 닫기", onclick: () => setOpen(false) }, "✕"));
  let body;
  if (state.tab === "ai") body = viewAi();
  else if (!LS.get(NICK_KEY) || state.view === "nick") body = viewNick();
  else body = { list: viewList, create: viewCreate, room: viewRoom, settings: viewSettings, password: viewPassword }[state.view]();
  panel.replaceChildren(tabs, body, h("div.chat-toast", { hidden: true }));
  if (state.view === "room" && state.tab === "chat") {
    const box = panel.querySelector(".chat-msgs");
    if (box) box.scrollTop = box.scrollHeight;
    panel.querySelector(".chat-input textarea")?.focus();
  }
}

function viewAi() {
  return h("div.chat-body.chat-ai",
    h("div.chat-empty",
      h("b", {}, "AI 비서는 준비 중입니다"),
      h("p", {}, "뉴스·공시 원문을 근거로 종목 질문에 답하고, 답마다 출처를 함께 보여주는 비서를 만들고 있습니다.")));
}

function viewNick() {
  const L = state.limits || { nickMin: 2, nickMax: 12 };
  const input = h("input", { maxlength: L.nickMax, placeholder: `닉네임 (${L.nickMin}~${L.nickMax}자)`,
                             value: LS.get(NICK_KEY) || "", "aria-label": "닉네임" });
  const err = h("p.chat-err", { text: state.nickError || "" });
  const save = (e) => {
    e.preventDefault();
    const v = input.value.trim().replace(/\s+/g, " ");
    if ([...v].length < L.nickMin || [...v].length > L.nickMax) { err.textContent = `닉네임은 ${L.nickMin}~${L.nickMax}자입니다`; return; }
    LS.set(NICK_KEY, v); state.nickError = null;
    if (state.joinTarget && state.view === "nick") connect(state.joinTarget.room, state.joinTarget.password);
    else go("list");
  };
  return h("form.chat-body.chat-form", { onsubmit: save },
    h("h3", {}, "채팅에서 쓸 닉네임"),
    input, err,
    h("button.chat-primary", { type: "submit" }, "확인"),
    h("p.chat-hint", {}, "계정 없이 이 브라우저에 저장됩니다. 다른 사람을 사칭하거나 불쾌감을 주는 닉네임은 쓰지 마세요."));
}

function viewList() {
  const L = state.limits;
  const items = state.rooms.map((r) => h("li",
    h("button.chat-room", { type: "button", onclick: () => enterRoom(r),
                            disabled: r.members >= r.capacity ? true : undefined },
      h("span.chat-room-title", {}, r.locked ? "🔒 " : "", r.title, r.permanent ? h("span.chat-tag", {}, "기본") : null),
      h("span.chat-room-count", { class: r.members >= r.capacity ? "full" : "" }, `${r.members}/${r.capacity}`))));
  return h("div.chat-body",
    h("div.chat-bar",
      h("span.chat-me", {}, "닉네임 ", h("b", {}, LS.get(NICK_KEY)),
        h("button.chat-link", { type: "button", onclick: () => { state.joinTarget = null; state.view = "nick"; render(); } }, "변경")),
      h("button.chat-primary.small", { type: "button", onclick: () => go("create"),
                                       disabled: L && state.rooms.length >= L.maxRooms ? true : undefined }, "+ 방 만들기")),
    state.notice ? h("p.chat-notice", {}, state.notice) : null,
    state.listError ? h("p.chat-err", {}, state.listError) : null,
    h("ul.chat-rooms", {}, items.length ? items : h("li.chat-empty", {}, "불러오는 중…")),
    L ? h("p.chat-hint", {}, `방은 최대 ${L.maxRooms}개, 방마다 최대 ${L.maxCapacity}명 · 빈 방은 6시간 뒤 자동으로 정리됩니다`) : null);
}

function enterRoom(r) {
  state.notice = null;
  state.retry = 0;
  if (r.locked && !LS.get(ownerKey(r.id))) { state.joinTarget = { room: r }; state.passwordError = null; state.view = "password"; return render(); }
  connect(r);
}

function viewPassword() {
  const r = state.joinTarget.room;
  const input = h("input", { type: "password", placeholder: "비밀번호", autocomplete: "off", "aria-label": "방 비밀번호" });
  return h("form.chat-body.chat-form", { onsubmit: (e) => { e.preventDefault(); state.passwordError = null; connect(r, input.value); } },
    h("div.chat-bar", h("button.chat-link", { type: "button", onclick: () => go("list") }, "← 목록")),
    h("h3", {}, "🔒 ", r.title),
    input,
    h("p.chat-err", { text: state.passwordError || "" }),
    h("button.chat-primary", { type: "submit" }, "입장"));
}

function viewCreate() {
  const L = state.limits || { titleMax: 30, minCapacity: 2, maxCapacity: 30, defaultCapacity: 20, passwordMin: 4, passwordMax: 20 };
  const title = h("input", { maxlength: L.titleMax, placeholder: `방 제목 (${L.titleMax}자까지)`, "aria-label": "방 제목" });
  const cap = h("input", { type: "number", min: L.minCapacity, max: L.maxCapacity, value: L.defaultCapacity, "aria-label": "정원" });
  const pw = h("input", { type: "password", maxlength: L.passwordMax, autocomplete: "new-password",
                          placeholder: `비밀번호 (선택, ${L.passwordMin}~${L.passwordMax}자)`, "aria-label": "비밀번호" });
  const err = h("p.chat-err");
  const btn = h("button.chat-primary", { type: "submit" }, "만들고 입장");
  const submit = async (e) => {
    e.preventDefault();
    btn.disabled = true; err.textContent = "";
    try {
      const b = await api("/rooms", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: title.value, capacity: Number(cap.value), password: pw.value || undefined, client: clientId() }) });
      LS.set(ownerKey(b.room.id), b.ownerToken);
      state.retry = 0;
      connect(b.room, pw.value || undefined);
    } catch (ex) { err.textContent = ex.message; btn.disabled = false; }
  };
  return h("form.chat-body.chat-form", { onsubmit: submit },
    h("div.chat-bar", h("button.chat-link", { type: "button", onclick: () => go("list") }, "← 목록")),
    h("h3", {}, "새 채팅방"),
    h("label", {}, "제목", title),
    h("label", {}, `정원 (${L.minCapacity}~${L.maxCapacity}명)`, cap),
    h("label", {}, "비밀번호", pw),
    err, btn,
    h("p.chat-hint", {}, "방장 권한(제목·비밀번호·정원 변경, 삭제)은 이 브라우저에 저장됩니다."));
}

function viewRoom() {
  const head = h("div.chat-roomhead",
    h("button.chat-link", { type: "button", "aria-label": "방 나가기", onclick: () => { disconnect(); go("list"); } }, "←"),
    h("div.chat-roominfo", h("b.chat-roomtitle"), h("span.chat-roommeta")),
    h("button.chat-link.chat-gear", { type: "button", "aria-label": "방 설정", hidden: true, onclick: () => { state.view = "settings"; render(); } }, "⚙"));
  const msgs = h("div.chat-msgs", { role: "log", "aria-live": "polite" });
  for (const m of state.messages || []) msgs.append(msgNode(m));
  const ta = h("textarea", { rows: 1, maxlength: state.limits?.messageMax || 300, placeholder: state.me ? "메시지 입력 (Enter 전송, Shift+Enter 줄바꿈)" : "",
                             "aria-label": "메시지", disabled: state.me ? undefined : true });
  const sendNow = () => {
    const text = ta.value.trim();
    if (!text) return;
    send({ t: "msg", text });
    ta.value = ""; ta.style.height = "";
  };
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendNow(); }
  });
  ta.addEventListener("input", () => { ta.style.height = ""; ta.style.height = Math.min(ta.scrollHeight, 96) + "px"; });
  const wrap = h("div.chat-body.chat-roomview", head,
    state.status ? h("p.chat-notice", {}, state.status) : null,
    msgs,
    h("form.chat-input", { onsubmit: (e) => { e.preventDefault(); sendNow(); } },
      ta, h("button.chat-primary.small", { type: "submit", disabled: state.me ? undefined : true }, "전송")));
  queueMicrotask(paintRoomHead);
  return wrap;
}

function paintRoomHead() {
  const r = state.room;
  if (!r || !panel.querySelector(".chat-roomtitle")) return;
  panel.querySelector(".chat-roomtitle").textContent = (r.locked ? "🔒 " : "") + r.title;
  panel.querySelector(".chat-roommeta").textContent = `${state.members.length}/${r.capacity}명`;
  panel.querySelector(".chat-roommeta").title = state.members.join(", ");
  panel.querySelector(".chat-gear").hidden = !state.me?.owner;
}

function msgNode(m) {
  if (m.kind === "sys") return h("div.chat-sys", {}, m.text);
  const mine = state.me && m.ch === state.me.ch;
  return h(`div.chat-msg${mine ? ".mine" : ""}`,
    mine ? null : h("span.chat-nick", {}, m.nick),
    h("div.chat-bubble-row",
      h("div.chat-bubble", {}, m.text),
      h("time.chat-time", { datetime: new Date(m.ts).toISOString() }, fmtTime(m.ts))));
}

function appendMessage(m) {
  const box = panel.querySelector(".chat-msgs");
  if (!box || state.view !== "room") return;
  const nearEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  box.append(msgNode(m));
  while (box.children.length > 200) box.firstChild.remove();
  if (nearEnd || (state.me && m.ch === state.me.ch)) box.scrollTop = box.scrollHeight;
}

function paintToast() {
  const t = panel.querySelector(".chat-toast");
  if (!t) return;
  t.textContent = state.toast; t.hidden = false;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => { t.hidden = true; }, 3500);
}

function viewSettings() {
  const r = state.room;
  const L = state.limits || { titleMax: 30, minCapacity: 2, maxCapacity: 30, passwordMin: 4, passwordMax: 20 };
  const title = h("input", { maxlength: L.titleMax, value: r.title, "aria-label": "방 제목" });
  const cap = h("input", { type: "number", min: L.minCapacity, max: L.maxCapacity, value: r.capacity, "aria-label": "정원" });
  const pwMode = h("select", { "aria-label": "비밀번호 설정" },
    h("option", { value: "keep" }, r.locked ? "비밀번호 유지" : "공개방 유지"),
    h("option", { value: "set" }, r.locked ? "비밀번호 바꾸기" : "비밀번호 걸기"),
    r.locked ? h("option", { value: "clear" }, "비밀번호 해제 (공개방)") : null);
  const pw = h("input", { type: "password", maxlength: L.passwordMax, autocomplete: "new-password",
                          placeholder: `새 비밀번호 (${L.passwordMin}~${L.passwordMax}자)`, hidden: true, "aria-label": "새 비밀번호" });
  pwMode.addEventListener("change", () => { pw.hidden = pwMode.value !== "set"; });
  const owner = LS.get(ownerKey(r.id));
  const save = (e) => {
    e.preventDefault();
    const patch = { t: "update", owner };
    if (title.value.trim() !== r.title) patch.title = title.value;
    if (Number(cap.value) !== r.capacity) patch.capacity = Number(cap.value);
    if (pwMode.value === "set") patch.password = pw.value;
    if (pwMode.value === "clear") patch.password = "";
    send(patch);
    state.view = "room"; render();
  };
  const del = h("button.chat-danger", { type: "button", hidden: r.permanent ? true : undefined }, "방 삭제");
  del.addEventListener("click", () => {
    if (del.dataset.confirm) { send({ t: "delete", owner }); return; }
    del.dataset.confirm = "1"; del.textContent = "한 번 더 누르면 삭제됩니다";       // confirm() 대화상자 대신 두 번 누르기
  });
  return h("form.chat-body.chat-form", { onsubmit: save },
    h("div.chat-bar", h("button.chat-link", { type: "button", onclick: () => { state.view = "room"; render(); } }, "← 대화로")),
    h("h3", {}, "방 설정"),
    h("label", {}, "제목", title),
    h("label", {}, `정원 (${L.minCapacity}~${L.maxCapacity}명)`, cap),
    h("label", {}, "비밀번호", pwMode, pw),
    h("button.chat-primary", { type: "submit" }, "저장"),
    del);
}
