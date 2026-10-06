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
  per: ["per"],
  pbr: ["pbr"],
  eps: ["eps"],
  bps: ["bps"],
  roe: ["roe"],          // eps/bps*100 파생값 (로드 시 계산)
  dividend: ["dividendYield", "배당", "배당률", "배당수익률"],
  dps: ["dps", "배당금", "주당배당금"],           // price*배당률 파생
  foreign: ["foreignRate", "외국인", "외인", "외국인비율"],
  high52: ["high52w", "고가52", "최고52"],
  low52: ["low52w", "저가52", "최저52"],
  pos52: ["pos52", "위치52", "주가위치"],                   // (가격-저가)/(고저폭)*100 파생
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
};

function tokenize(src) {
  const tokens = [];
  const re = /\s*(?:(\d+(?:\.\d+)?)|([A-Za-z가-힣_][A-Za-z가-힣0-9_]*)|([-+*/^(),]))/gy;
  let pos = 0;
  while (pos < src.length) {
    re.lastIndex = pos;
    const m = re.exec(src);
    if (!m || m.index !== pos) {
      // 공백만 남았으면 종료, 아니면 인식 불가 문자
      if (/^\s*$/.test(src.slice(pos))) break;
      throw new Error(`수식 오류: ${pos + 1}번째 문자 '${src[pos]}'를 해석할 수 없습니다`);
    }
    if (m[1] !== undefined) tokens.push({ t: "num", v: parseFloat(m[1]) });
    else if (m[2] !== undefined) tokens.push({ t: "id", v: m[2] });
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

/* 수식 팔레트: 스크리너에서 끌어다 쓰는 블록 목록.
   숫자로 계산할 수 있는 항목만 넣는다 - 업종·종목명 같은 문자 항목은 수식에 쓸 수 없으므로 없다.
   insert는 수식에 실제로 들어가는 토큰, label은 칩에 보이는 글자.
   (tests/formula.test.mjs가 모든 항목이 실제로 컴파일되는지 검사한다) */
export const FORMULA_PALETTE = [
  { group: "시세", kind: "field", items: [
    { label: "가격", tip: "현재가(원)" },
    { label: "등락률", tip: "전일 대비 등락률(%)" },
    { label: "거래량", tip: "누적 거래량(주)" },
    { label: "거래대금", tip: "누적 거래대금(원)" },
    { label: "시총", tip: "시가총액(원)" },
  ] },
  { group: "가치", kind: "field", items: [
    { label: "PER", tip: "주가수익비율" },
    { label: "PBR", tip: "주가순자산비율" },
    { label: "EPS", tip: "주당순이익(원)" },
    { label: "BPS", tip: "주당순자산(원)" },
    { label: "ROE", tip: "EPS÷BPS×100 (%)" },
  ] },
  { group: "배당·수급", kind: "field", items: [
    { label: "배당률", tip: "배당수익률(%)" },
    { label: "배당금", tip: "주당배당금 추정(원)" },
    { label: "외국인", tip: "외국인 보유비율(%)" },
  ] },
  { group: "재무(추정)", kind: "field", items: [
    { label: "순이익", tip: "EPS×주식수(원)" },
    { label: "순자산", tip: "BPS×주식수(원)" },
    { label: "주식수", tip: "시총÷가격(주)" },
  ] },
  { group: "52주", kind: "field", items: [
    { label: "고가52", tip: "52주 최고가(원)" },
    { label: "저가52", tip: "52주 최저가(원)" },
    { label: "위치52", tip: "52주 범위 안 현재 위치(0~100)" },
  ] },
  { group: "연산", kind: "op", items: [
    { label: "+", insert: "+", tip: "더하기" },
    { label: "−", insert: "-", tip: "빼기" },
    { label: "×", insert: "*", tip: "곱하기" },
    { label: "÷", insert: "/", tip: "나누기" },
    { label: "^", insert: "^", tip: "거듭제곱" },
    { label: "(", insert: "(", tip: "여는 괄호" },
    { label: ")", insert: ")", tip: "닫는 괄호" },
    { label: ",", insert: ",", tip: "함수 인자 구분 (min·max)" },
  ] },
  { group: "함수", kind: "func", items: [
    { label: "abs", insert: "abs(", tip: "절댓값" },
    { label: "log", insert: "log(", tip: "자연로그" },
    { label: "sqrt", insert: "sqrt(", tip: "제곱근" },
    { label: "min", insert: "min(", tip: "최솟값: min(a, b)" },
    { label: "max", insert: "max(", tip: "최댓값: max(a, b)" },
  ] },
];
for (const g of FORMULA_PALETTE) for (const it of g.items) it.insert ??= it.label;
