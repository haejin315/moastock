/* 사용자 수식 엔진 — eval() 없이 동작하는 재귀 하강 파서.
   지원: + - * / ^ 괄호, 단항 -, 숫자, 필드 변수(한/영), 함수 abs/log/sqrt/min/max.
   필드값이 없는 종목(예: 적자로 PER 없음)은 NaN → 정렬 시 맨 뒤로 보낸다. */
"use strict";

const FORMULA_FIELDS = {
  // 표시명: [필드키, 별칭들]
  price: ["price", "가격", "주가", "현재가"],
  change: ["change", "등락률", "등락"],
  volume: ["volume", "거래량"],
  value: ["value", "거래대금"],
  marketcap: ["marketCap", "시총", "시가총액"],
  per: ["per", "주가수익비율"],
  pbr: ["pbr", "주가순자산비율"],
  eps: ["eps", "주당순이익"],
  bps: ["bps", "주당순자산"],
  roe: ["roe", "자기자본이익률"],          // eps/bps*100 파생값 (로드 시 계산)
  dividend: ["dividendYield", "배당", "배당률", "배당수익률"],
  dps: ["dps", "배당금", "주당배당금"],           // price*배당률 파생
  foreign: ["foreignRate", "외국인", "외인", "외국인비율", "외국인보유비율"],
  high52: ["high52w", "고가52", "최고52", "52주최고가"],
  low52: ["low52w", "저가52", "최저52", "52주최저가"],
  pos52: ["pos52", "위치52", "주가위치", "52주위치"],                   // (가격-저가)/(고저폭)*100 파생
  shares: ["shares", "주식수", "상장주식수"],      // 시총/가격 파생
  netincome: ["netIncome", "순이익", "순익"],      // EPS*주식수 파생
  equity: ["equity", "순자산", "자본", "자본총계"], // BPS*주식수 파생
};

const ALIAS_TO_KEY = (() => {
  const map = new Map();
  for (const [canon, [key, ...aliases]] of Object.entries(FORMULA_FIELDS)) {
    map.set(canon.toLowerCase(), key);
    map.set(key.toLowerCase(), key);
    for (const a of aliases) map.set(a.toLowerCase(), key);
  }
  return map;
})();

const FUNCS = {
  abs: Math.abs, log: Math.log, sqrt: Math.sqrt,
  min: Math.min, max: Math.max,
  // 한글 함수명
  "절댓값": Math.abs, "자연로그": Math.log, "제곱근": Math.sqrt, "최솟값": Math.min, "최댓값": Math.max,
};

// 식별자: 일반 이름, 또는 "52주최고가"처럼 숫자+주로 시작하는 이름(숫자보다 먼저 시도) / 숫자 / 연산자
const TOKEN_RE = /\s*(?:(\d+주[A-Za-z가-힣0-9_]*|[A-Za-z가-힣_][A-Za-z가-힣0-9_]*)|(\d+(?:\.\d+)?)|([-+*/^(),]))/y;

function tokenize(src) {
  const tokens = [];
  const re = TOKEN_RE;
  let pos = 0;
  while (pos < src.length) {
    re.lastIndex = pos;
    const m = re.exec(src);
    if (!m || m.index !== pos) {
      // 공백만 남았으면 종료, 아니면 인식 불가 문자
      if (/^\s*$/.test(src.slice(pos))) break;
      throw new Error(`수식 오류: ${pos + 1}번째 문자 '${src[pos]}'를 해석할 수 없습니다`);
    }
    if (m[1] !== undefined) tokens.push({ t: "id", v: m[1] });
    else if (m[2] !== undefined) tokens.push({ t: "num", v: parseFloat(m[2]) });
    else tokens.push({ t: m[3] });
    pos = re.lastIndex;
  }
  return tokens;
}

/* 문법: expr := term (('+'|'-') term)*
         term := factor (('*'|'/') factor)*
         factor := unary ('^' factor)?          (거듭제곱은 우결합)
         unary := '-' unary | primary
         primary := num | id | id '(' args ')' | '(' expr ')'          */
export function compileFormula(src) {
  const tokens = tokenize(String(src || ""));
  if (!tokens.length) throw new Error("수식이 비어 있습니다");
  let i = 0;
  const peek = () => tokens[i];
  const eat = (t) => {
    if (!tokens[i] || tokens[i].t !== t) {
      throw new Error(`수식 오류: '${t}'가 필요합니다`);
    }
    return tokens[i++];
  };

  const usedFields = new Set();

  function primary() {
    const tok = peek();
    if (!tok) throw new Error("수식이 갑자기 끝났습니다");
    if (tok.t === "num") { i++; return () => tok.v; }
    if (tok.t === "(") {
      i++;
      const inner = expr();
      eat(")");
      return inner;
    }
    if (tok.t === "id") {
      i++;
      const name = tok.v.toLowerCase();
      if (peek() && peek().t === "(") {
        const fn = FUNCS[name];
        if (!fn) throw new Error(`알 수 없는 함수: ${tok.v}`);
        i++; // '('
        const args = [expr()];
        while (peek() && peek().t === ",") { i++; args.push(expr()); }
        eat(")");
        return (row) => fn(...args.map((a) => a(row)));
      }
      const key = ALIAS_TO_KEY.get(name);
      if (!key) throw new Error(`알 수 없는 항목: ${tok.v}`);
      usedFields.add(key);
      return (row) => {
        const v = row[key];
        return v === null || v === undefined ? NaN : v;
      };
    }
    throw new Error(`수식 오류: '${tok.t}' 위치를 해석할 수 없습니다`);
  }

  function unary() {
    if (peek() && peek().t === "-") { i++; const u = unary(); return (r) => -u(r); }
    return primary();
  }
  function factor() {
    const base = unary();
    if (peek() && peek().t === "^") { i++; const exp = factor(); return (r) => Math.pow(base(r), exp(r)); }
    return base;
  }
  function term() {
    let left = factor();
    while (peek() && (peek().t === "*" || peek().t === "/")) {
      const op = tokens[i++].t;
      const right = factor();
      const l = left;
      left = op === "*" ? (r) => l(r) * right(r) : (r) => l(r) / right(r);
    }
    return left;
  }
  function expr() {
    let left = term();
    while (peek() && (peek().t === "+" || peek().t === "-")) {
      const op = tokens[i++].t;
      const right = term();
      const l = left;
      left = op === "+" ? (r) => l(r) + right(r) : (r) => l(r) - right(r);
    }
    return left;
  }

  const compiled = expr();
  if (i < tokens.length) throw new Error("수식 끝에 해석할 수 없는 부분이 있습니다");
  return { evaluate: compiled, fields: [...usedFields] };
}

// 브라우저 전역 + Node(test) 겸용
if (typeof window !== "undefined") window.compileFormula = compileFormula;

/* 수식 블록: 스크리너에서 끌어다 쓰거나, 수식 칸에 입력하면 자동으로 바뀌는 블록.
   숫자로 계산할 수 있는 항목만 있다 - 업종·종목명 같은 문자 항목은 수식에 쓸 수 없으므로 없다.
   label: 블록에 보이는 이름(줄임말 없이), insert: 수식 문자열에 들어가는 토큰.
   (tests/formula.test.mjs가 모든 항목이 실제로 계산되는지 검사한다) */
export const FORMULA_PALETTE = [
  { group: "시세", kind: "field", items: [
    { label: "현재가", insert: "현재가", tip: "현재 주가(원)" },
    { label: "등락률", insert: "등락률", tip: "전일 대비 등락률(%)" },
    { label: "거래량", insert: "거래량", tip: "당일 누적 거래량(주)" },
    { label: "거래대금", insert: "거래대금", tip: "당일 누적 거래대금(원)" },
    { label: "시가총액", insert: "시가총액", tip: "시가총액(원)" },
  ] },
  { group: "가치", kind: "field", items: [
    { label: "주가수익비율(PER)", insert: "주가수익비율", tip: "주가 ÷ 주당순이익" },
    { label: "주가순자산비율(PBR)", insert: "주가순자산비율", tip: "주가 ÷ 주당순자산" },
    { label: "주당순이익(EPS)", insert: "주당순이익", tip: "순이익 ÷ 상장주식수(원)" },
    { label: "주당순자산(BPS)", insert: "주당순자산", tip: "순자산 ÷ 상장주식수(원)" },
    { label: "자기자본이익률(ROE)", insert: "자기자본이익률", tip: "주당순이익 ÷ 주당순자산 × 100 (%)" },
  ] },
  { group: "배당·수급", kind: "field", items: [
    { label: "배당수익률", insert: "배당수익률", tip: "주당배당금 ÷ 현재가 × 100 (%)" },
    { label: "주당배당금", insert: "주당배당금", tip: "현재가 × 배당수익률로 추정(원)" },
    { label: "외국인보유비율", insert: "외국인보유비율", tip: "외국인이 보유한 주식 비율(%)" },
  ] },
  { group: "재무(추정)", kind: "field", items: [
    { label: "순이익", insert: "순이익", tip: "주당순이익 × 상장주식수(원)" },
    { label: "순자산", insert: "순자산", tip: "주당순자산 × 상장주식수(원)" },
    { label: "상장주식수", insert: "상장주식수", tip: "시가총액 ÷ 현재가(주)" },
  ] },
  { group: "52주 가격", kind: "field", items: [
    { label: "52주 최고가", insert: "52주최고가", tip: "최근 52주 최고가(원)" },
    { label: "52주 최저가", insert: "52주최저가", tip: "최근 52주 최저가(원)" },
    { label: "52주 위치", insert: "52주위치", tip: "52주 최저가~최고가 사이에서 현재가의 위치(0~100)" },
  ] },
  { group: "연산", kind: "op", items: [
    { label: "+", insert: "+", tip: "더하기" },
    { label: "−", insert: "-", tip: "빼기" },
    { label: "×", insert: "*", tip: "곱하기" },
    { label: "÷", insert: "/", tip: "나누기" },
    { label: "^", insert: "^", tip: "거듭제곱" },
    { label: "(", insert: "(", tip: "여는 괄호" },
    { label: ")", insert: ")", tip: "닫는 괄호" },
    { label: ",", insert: ",", tip: "함수 인자 구분 (최솟값·최댓값)" },
  ] },
  { group: "함수", kind: "func", items: [
    { label: "절댓값", insert: "절댓값", tip: "절댓값(x)" },
    { label: "자연로그", insert: "자연로그", tip: "자연로그(x)" },
    { label: "제곱근", insert: "제곱근", tip: "제곱근(x)" },
    { label: "최솟값", insert: "최솟값", tip: "최솟값(a, b)" },
    { label: "최댓값", insert: "최댓값", tip: "최댓값(a, b)" },
  ] },
];

const FUNC_ENGLISH = { "절댓값": "abs", "자연로그": "log", "제곱근": "sqrt", "최솟값": "min", "최댓값": "max" };
const FIELD_BLOCK = new Map();          // 필드키 → 블록
const FUNC_BLOCK = new Map();           // 함수 이름(소문자, 한/영) → 블록
const OP_BLOCK = new Map();             // 연산 기호 → 블록
for (const g of FORMULA_PALETTE) {
  for (const it of g.items) {
    it.kind = g.kind;
    if (g.kind === "field") FIELD_BLOCK.set(ALIAS_TO_KEY.get(it.insert.toLowerCase()), it);
    if (g.kind === "func") { FUNC_BLOCK.set(it.insert, it); FUNC_BLOCK.set(FUNC_ENGLISH[it.insert], it); }
    if (g.kind === "op") OP_BLOCK.set(it.insert, it);
  }
}

/* 입력 중 블록으로 바꿀 단어 사전: 모든 별칭(영문 약어 포함)·정식 명칭·띄어 쓴 이름.
   word는 소문자. 긴 단어가 먼저 오도록 정렬해 가장 긴 일치를 고른다. */
export const FORMULA_KEYWORDS = (() => {
  const out = new Map();
  const add = (word, block) => { if (word) out.set(word.toLowerCase(), block); };
  for (const [canon, [key, ...aliases]] of Object.entries(FORMULA_FIELDS)) {
    const block = FIELD_BLOCK.get(key);
    if (!block) continue;
    // 괄호 붙은 표시명("주가수익비율(PER)")은 넣지 않는다 - 입력한 '('와 겹친다
    for (const w of [canon, key, ...aliases, block.insert, block.label.replace(/\(.*\)$/, "")]) {
      add(w, block);
    }
  }
  for (const [name, block] of FUNC_BLOCK) add(name, block);
  return [...out].map(([word, block]) => ({ word, block })).sort((a, b) => b.word.length - a.word.length);
})();

/** 입력 문자 → 연산 기호 (× ÷ − 같은 표시 기호도 받는다) */
export const OP_CHARS = {
  "+": "+", "-": "-", "−": "-", "*": "*", "×": "*", "/": "/", "÷": "/", "^": "^", "(": "(", ")": ")", ",": ",",
};

export const opBlock = (op) => OP_BLOCK.get(op);
export const numBlock = (text) => ({ kind: "num", label: text, insert: text, tip: "숫자" });

/** 수식 문자열 → 블록 목록. 알 수 없는 이름은 {kind:"text"}로 남겨 편집기에서 오류로 보이게 한다. */
export function formulaToBlocks(src) {
  const out = [];
  const s = String(src || "");
  let pos = 0;
  while (pos < s.length) {
    TOKEN_RE.lastIndex = pos;
    const m = TOKEN_RE.exec(s);
    if (!m) {
      if (!/^\s*$/.test(s.slice(pos))) out.push({ kind: "text", text: s.slice(pos).trim() });
      break;
    }
    pos = TOKEN_RE.lastIndex;
    if (m[1] !== undefined) {
      const low = m[1].toLowerCase();
      const key = ALIAS_TO_KEY.get(low);
      const block = key ? FIELD_BLOCK.get(key) : FUNC_BLOCK.get(low);
      out.push(block || { kind: "text", text: m[1] });
    } else if (m[2] !== undefined) {
      out.push(numBlock(m[2]));
    } else {
      out.push(OP_BLOCK.get(m[3]));
    }
  }
  return out;
}

/** 블록 목록 → 수식 문자열 (편집기에 남은 글자도 그대로 포함 → 컴파일 시 오류로 잡힌다) */
export function blocksToFormula(blocks) {
  let out = "";
  for (const b of blocks) {
    const t = b.kind === "text" ? b.text : b.insert;
    if (!t) continue;
    const tight = /^[),]$/.test(t) || out.endsWith("(") || (t === "(" && /[가-힣A-Za-z0-9_]$/.test(out));
    out += (out && !tight ? " " : "") + t;
  }
  return out.trim();
}

/** 표기 통일: 줄임말·영문(PER, 시총, log …)으로 쓴 수식을 정식 명칭 표기로 바꾼다.
    해석할 수 없는 단어가 있으면 원문을 그대로 돌려준다(오류 메시지는 컴파일 단계에서). */
export function normalizeFormula(src) {
  const blocks = formulaToBlocks(src);
  return blocks.some((b) => b.kind === "text") ? String(src || "").trim() : blocksToFormula(blocks);
}
