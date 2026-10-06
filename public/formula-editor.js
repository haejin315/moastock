/* 수식 블록 편집기 (contenteditable).
   - 키보드로 입력한 항목·함수 이름·숫자·연산 기호는 단어가 완성되면 자동으로 블록이 된다
     (뒤에 띄어쓰기나 연산 기호가 오면 완성. "배당"처럼 더 긴 이름의 앞부분이면 기다린다)
   - 블록이 되지 못하고 남은 글자는 수식에 쓸 수 없는 단어 → 그대로 글자로 남아 오류로 보인다
   - 한글 조합 중에는 손대지 않고, 조합이 끝난 뒤에 바꾼다
   - 팔레트 블록 끌어다 놓기 / 클릭 삽입, 블록 단위 지우기·화살표 이동 지원

   내부 모델: 편집기 내용 = 항목 목록 [{chip: 블록} | {text: 문자열}]
   위치(캐럿)는 "선형 좌표"로 다룬다: 블록 하나 = 1칸, 글자 하나 = 1칸.
   블록 뒤 캐럿이 놓일 자리를 만들려고 보이지 않는 문자(ZWSP)를 끼워 넣지만 좌표에서는 세지 않는다. */
import { FORMULA_KEYWORDS, OP_CHARS, opBlock, numBlock, formulaToBlocks, blocksToFormula } from "./formula.js";

export const PALETTE_MIME = "application/x-moastock-block";
const ZW = "​";
const IDENT = /[A-Za-z가-힣0-9_]/;

const isDelim = (ch) => ch === undefined || /\s/.test(ch) || ch in OP_CHARS;

/** s의 i 위치에서 시작하는 키워드 일치 (가장 긴 것). final이면 문자열 끝도 완성으로 본다. */
function matchKeyword(s, i, final) {
  if (i > 0 && IDENT.test(s[i - 1])) return null;           // 단어 중간에서는 시작하지 않는다
  const low = s.toLowerCase();
  for (const { word, block } of FORMULA_KEYWORDS) {
    if (!low.startsWith(word, i)) continue;
    const next = s[i + word.length];
    if (next !== undefined) return isDelim(next) ? { len: word.length, block } : null;
    // 문자열 끝: 더 긴 이름의 앞부분이면(예: "배당" → "배당수익률") 아직 기다린다
    const typed = low.slice(i);
    const longer = FORMULA_KEYWORDS.some((k) => k.word.length > typed.length && k.word.startsWith(typed));
    return final || !longer ? { len: word.length, block } : null;
  }
  return null;
}

function matchNumber(s, i, final) {
  if (i > 0 && IDENT.test(s[i - 1])) return null;
  const m = /^\d+(?:\.\d+)?/.exec(s.slice(i));
  if (!m) return null;
  const next = s[i + m[0].length];
  if (next !== undefined && !isDelim(next)) return null;     // "52주…"처럼 이어지는 단어
  if (next === undefined && !final) return null;             // 아직 숫자를 치는 중일 수 있다
  return { len: m[0].length, block: numBlock(m[0]) };
}

/** 글자 조각을 블록으로 바꾼다. 반환: [{chip}|{text}] 조각과 각 조각이 차지한 원래 길이 */
function convertText(s, final) {
  const pieces = [];
  let buf = "";
  const flush = () => { if (buf) { pieces.push({ text: buf, span: buf.length }); buf = ""; } };
  for (let i = 0; i < s.length;) {
    const ch = s[i];
    if (ch in OP_CHARS) { flush(); pieces.push({ chip: opBlock(OP_CHARS[ch]), span: 1 }); i++; continue; }
    const hit = matchKeyword(s, i, final) || matchNumber(s, i, final);
    if (hit) { flush(); pieces.push({ chip: hit.block, span: hit.len }); i += hit.len; continue; }
    buf += ch; i++;
  }
  flush();
  return pieces;
}

export function createFormulaEditor(el, { placeholder = "", onChange = () => {}, onEnter = () => {} } = {}) {
  el.contentEditable = "true";
  el.spellcheck = false;
  el.setAttribute("role", "textbox");
  el.setAttribute("aria-multiline", "false");
  el.dataset.placeholder = placeholder;
  let composing = false;
  let lastCaret = 0;

  // ---- DOM ↔ 모델 -------------------------------------------------------------
  const chipNode = (b) => {
    const n = document.createElement("span");
    n.className = `fchip ${b.kind}`;
    n.contentEditable = "false";
    n.textContent = b.label;
    n.title = b.tip || "";
    n.dataset.block = JSON.stringify({ kind: b.kind, label: b.label, insert: b.insert, tip: b.tip });
    return n;
  };
  const blockOf = (n) => JSON.parse(n.dataset.block);
  const isChip = (n) => n.nodeType === 1 && n.classList.contains("fchip");

  function readItems() {
    const items = [];
    for (const n of el.childNodes) {
      if (isChip(n)) items.push({ chip: blockOf(n) });
      else if (n.nodeName === "BR") continue;
      else {
        const t = (n.textContent || "").replaceAll(ZW, "").replace(/ /g, " ");
        if (!t) continue;
        const last = items.at(-1);
        if (last && "text" in last) last.text += t; else items.push({ text: t });
      }
    }
    return items;
  }

  function render(items, caret) {
    const nodes = [];
    for (const it of items) {
      if (it.chip) {
        if (!nodes.length || isChip(nodes.at(-1))) nodes.push(document.createTextNode(ZW));
        nodes.push(chipNode(it.chip));
      } else {
        nodes.push(document.createTextNode((nodes.length && isChip(nodes.at(-1)) ? ZW : "") + it.text));
      }
    }
    if (nodes.length && isChip(nodes.at(-1))) nodes.push(document.createTextNode(ZW));
    el.replaceChildren(...nodes);
    refreshEmpty();
    if (caret !== null && caret !== undefined && document.activeElement === el) setCaret(caret);
  }

  const itemLen = (it) => (it.chip ? 1 : it.text.length);
  const nodeLen = (n) => (isChip(n) ? 1 : (n.textContent || "").replaceAll(ZW, "").length);

  /** (node, offset) → 선형 좌표 */
  function linearOf(node, offset) {
    if (node === el) {
      let acc = 0;
      for (let i = 0; i < offset && i < el.childNodes.length; i++) acc += nodeLen(el.childNodes[i]);
      return acc;
    }
    let acc = 0;
    for (const n of el.childNodes) {
      if (n === node || n.contains(node)) {
        if (isChip(n)) return acc + 1;
        return acc + (n.textContent || "").slice(0, offset).replaceAll(ZW, "").length;
      }
      acc += nodeLen(n);
    }
    return acc;
  }

  function caretLinear() {
    const sel = getSelection();
    if (!sel.rangeCount || !el.contains(sel.anchorNode)) return null;
    return linearOf(sel.focusNode, sel.focusOffset);
  }

  function setCaret(lin) {
    const sel = getSelection();
    const r = document.createRange();
    let acc = 0;
    for (const n of el.childNodes) {
      const len = nodeLen(n);
      if (!isChip(n) && lin <= acc + len) {
        // 선형 오프셋 → 실제 오프셋 (ZWSP 건너뛰기)
        const raw = n.textContent;
        let eff = 0, i = 0;
        while (i < raw.length && (raw[i] === ZW || eff < lin - acc)) { if (raw[i] !== ZW) eff++; i++; }
        r.setStart(n, i);
        r.collapse(true);
        sel.removeAllRanges(); sel.addRange(r);
        lastCaret = lin;
        return;
      }
      acc += len;
    }
    r.selectNodeContents(el); r.collapse(false);
    sel.removeAllRanges(); sel.addRange(r);
    lastCaret = acc;
  }

  // ---- 자동 블록 변환 ---------------------------------------------------------
  /** 남은 글자를 블록으로 바꾼다. 바뀐 게 없으면 DOM을 건드리지 않는다(입력기 안정). */
  function convert(final = false) {
    const items = readItems();
    const caret = caretLinear();
    const out = [];
    let changed = false, oldPos = 0, newPos = 0, newCaret = caret;
    // 원래 [oldStart, oldStart+span) 조각이 새 길이 newLen이 될 때 캐럿 위치 옮기기
    const mapCaret = (oldStart, span, newLen) => {
      if (caret === null || caret < oldStart || caret > oldStart + span) return;
      if (newLen === span) newCaret = newPos + (caret - oldStart);   // 그대로 남은 글자
      else if (newLen === 0) newCaret = newPos;                       // 지워진 공백
      else newCaret = newPos + (caret > oldStart ? 1 : 0);           // 블록이 된 단어: 블록 뒤로
    };
    for (const it of items) {
      if (it.chip) { mapCaret(oldPos, 1, 1); out.push(it); oldPos++; newPos++; continue; }
      for (const p of convertText(it.text, final)) {
        if (p.chip) {
          changed = true;
          mapCaret(oldPos, p.span, 1);
          out.push({ chip: p.chip }); newPos++;
        } else if (/^\s+$/.test(p.text) && out.at(-1)?.chip) {
          // 블록 뒤 공백은 블록 간격으로 충분하므로 버린다
          changed = true;
          mapCaret(oldPos, p.span, 0);
        } else {
          mapCaret(oldPos, p.span, p.span);
          const last = out.at(-1);
          if (last && "text" in last) last.text += p.text; else out.push({ text: p.text });
          newPos += p.span;
        }
        oldPos += p.span;
      }
    }
    if (changed) render(out, newCaret);
    emit();
  }

  function emit() {
    refreshEmpty();
    onChange(value());
  }

  function refreshEmpty() {
    el.classList.toggle("empty", readItems().every((it) => "text" in it && !it.text.trim()));
  }

  function value() {
    return blocksToFormula(readItems().map((it) =>
      it.chip ? it.chip : { kind: "text", text: it.text.trim() }).filter((b) => b.kind !== "text" || b.text));
  }

  /** 블록이 아닌 글자 조각이 있는가, 그리고 그 조각이 지금 입력 중인 끝자리인가 */
  function leftover() {
    const items = readItems();
    const texts = items.filter((it) => "text" in it && it.text.trim());
    if (!texts.length) return { any: false };
    const last = items.at(-1);
    const typingAtEnd = texts.length === 1 && last === texts[0] && caretLinear() === items.reduce((a, it) => a + itemLen(it), 0);
    return { any: true, typingAtEnd, words: texts.map((t) => t.text.trim()) };
  }

  // ---- 삽입 -----------------------------------------------------------------
  function insertBlocks(blocks, at = lastCaret) {
    const items = readItems();
    const total = items.reduce((a, it) => a + itemLen(it), 0);
    let pos = Math.max(0, Math.min(at ?? total, total));
    // 선형 위치 pos에서 항목을 쪼개 블록을 끼운다
    const out = [];
    let acc = 0, done = false;
    const put = () => { for (const b of blocks) out.push({ chip: b }); done = true; };
    for (const it of items) {
      const len = itemLen(it);
      if (!done && pos <= acc) put();
      if (!done && "text" in it && pos > acc && pos < acc + len) {
        out.push({ text: it.text.slice(0, pos - acc) }); put(); out.push({ text: it.text.slice(pos - acc) });
      } else out.push(it);
      acc += len;
    }
    if (!done) put();
    el.focus();
    render(out, pos + blocks.length);
    lastCaret = pos + blocks.length;
    emit();
  }

  // ---- 이벤트 -----------------------------------------------------------------
  el.addEventListener("compositionstart", () => { composing = true; });
  el.addEventListener("compositionend", () => { composing = false; convert(); });
  el.addEventListener("input", (e) => { if (!composing && !e.isComposing) convert(); else emit(); });
  el.addEventListener("blur", () => convert(true));   // 칸을 떠나면 끝자리 단어도 마무리
  for (const ev of ["keyup", "mouseup", "focus"]) {
    el.addEventListener(ev, () => { const c = caretLinear(); if (c !== null) lastCaret = c; });
  }

  el.addEventListener("keydown", (e) => {
    if (composing || e.isComposing) return;
    if (e.key === "Enter") { e.preventDefault(); convert(true); onEnter(value()); return; }
    const sel = getSelection();
    if (!sel.isCollapsed) return;                                   // 범위 선택 삭제는 브라우저에 맡긴다
    const c = caretLinear();
    if (c === null) return;
    const items = readItems();
    const at = (lin) => {                                           // 선형 위치 lin에 있는 항목
      let acc = 0;
      for (let i = 0; i < items.length; i++) {
        const len = itemLen(items[i]);
        if (lin < acc + len) return { i, off: lin - acc };
        acc += len;
      }
      return null;
    };
    const removeChipAt = (lin) => {
      const hit = at(lin);
      if (!hit || !items[hit.i].chip) return false;
      items.splice(hit.i, 1);
      render(items, lin);
      emit();
      return true;
    };
    if (e.key === "Backspace" && c > 0 && removeChipAt(c - 1)) e.preventDefault();
    else if (e.key === "Delete" && removeChipAt(c)) e.preventDefault();
    else if (e.key === "ArrowLeft" && !e.shiftKey && c > 0) { e.preventDefault(); setCaret(c - 1); }
    else if (e.key === "ArrowRight" && !e.shiftKey) {
      const total = items.reduce((a, it) => a + itemLen(it), 0);
      if (c < total) { e.preventDefault(); setCaret(c + 1); }
    }
  });

  el.addEventListener("paste", (e) => {
    e.preventDefault();
    const text = (e.clipboardData.getData("text/plain") || "").replace(/[\r\n]+/g, " ");
    document.execCommand("insertText", false, text);
    convert(true);
  });

  // 팔레트 블록만 놓을 수 있다 (정렬 칩·외부 텍스트·파일 등은 거부)
  el.addEventListener("dragover", (e) => {
    if (!e.dataTransfer.types.includes(PALETTE_MIME)) { e.dataTransfer.dropEffect = "none"; return; }
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    el.classList.add("drop-ok");
  });
  el.addEventListener("dragleave", () => el.classList.remove("drop-ok"));
  el.addEventListener("drop", (e) => {
    e.preventDefault();
    el.classList.remove("drop-ok");
    const data = e.dataTransfer.getData(PALETTE_MIME);
    if (!data) return;
    const r = document.caretRangeFromPoint?.(e.clientX, e.clientY);
    const pos = r && el.contains(r.startContainer) ? linearOf(r.startContainer, r.startOffset) : undefined;
    insertBlocks(JSON.parse(data), pos);
  });

  render([], null);

  return {
    el,
    value,
    leftover,
    insertBlocks,
    finalize: () => convert(true),
    setValue(src) { render(formulaToBlocks(src).map((b) => (b.kind === "text" ? { text: b.text } : { chip: b })), null); emit(); },
    clear() { render([], null); lastCaret = 0; emit(); },
  };
}
