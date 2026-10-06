import test from "node:test";
import assert from "node:assert/strict";
import { compileFormula } from "../public/formula.js";

const row = {
  price: 70000, change: -1.5, volume: 1_000_000, value: 7e10,
  marketCap: 4e14, per: 10, pbr: 2, eps: 7000, bps: 35000,
  roe: 20, dividendYield: 2.5, foreignRate: 50, high52w: 90000, low52w: 50000,
};

test("기본 산술과 우선순위", () => {
  assert.equal(compileFormula("1 + 2 * 3").evaluate(row), 7);
  assert.equal(compileFormula("(1 + 2) * 3").evaluate(row), 9);
  assert.equal(compileFormula("2 ^ 3 ^ 2").evaluate(row), 512); // 우결합
  assert.equal(compileFormula("-PER + 12").evaluate(row), 2);
});

test("한국어/영문 별칭 모두 같은 필드로", () => {
  const a = compileFormula("등락률 * 거래대금").evaluate(row);
  const b = compileFormula("change * value").evaluate(row);
  assert.equal(a, b);
  assert.equal(compileFormula("시총").evaluate(row), row.marketCap);
});

test("가치 수식: 1/PER + 1/PBR", () => {
  const { evaluate, fields } = compileFormula("1/PER + 1/PBR");
  assert.equal(evaluate(row), 0.6);
  assert.deepEqual(fields.sort(), ["pbr", "per"]);
});

test("함수 호출", () => {
  assert.equal(compileFormula("abs(등락률)").evaluate(row), 1.5);
  assert.equal(compileFormula("max(PER, PBR, 15)").evaluate(row), 15);
  assert.ok(Math.abs(compileFormula("sqrt(거래량)").evaluate(row) - 1000) < 1e-9);
});

test("52주 위치 수식", () => {
  // (가격-저가52)/(고가52-저가52) = 0.5
  const v = compileFormula("(가격 - 저가52) / (고가52 - 저가52)").evaluate(row);
  assert.equal(v, 0.5);
});

test("결측 필드는 NaN (정렬 시 뒤로 보낼 수 있게)", () => {
  const v = compileFormula("1/PER").evaluate({ ...row, per: null });
  assert.ok(Number.isNaN(v));
});

test("나쁜 수식은 명확한 오류", () => {
  assert.throws(() => compileFormula("PER +"), /수식/);
  assert.throws(() => compileFormula("없는항목 * 2"), /알 수 없는 항목/);
  assert.throws(() => compileFormula("hack(1)"), /알 수 없는 함수/);
  assert.throws(() => compileFormula("PER; alert(1)"), /해석할 수 없습니다/);
  assert.throws(() => compileFormula(""), /비어/);
});

test("팔레트: 항목은 모두 수식에서 실제로 동작하고, 문자 항목은 없다", async () => {
  const { FORMULA_PALETTE } = await import("../public/formula.js");
  const fields = FORMULA_PALETTE.filter((g) => g.kind === "field").flatMap((g) => g.items);
  assert.ok(fields.length >= 15);
  for (const it of fields) {
    const v = compileFormula(it.insert).evaluate(row);
    assert.equal(typeof v, "number", `${it.label}`);
  }
  for (const it of FORMULA_PALETTE.find((g) => g.kind === "func").items) {
    const args = ["min", "max"].includes(it.label) ? "PER, PBR" : "PER";
    assert.doesNotThrow(() => compileFormula(`${it.insert}(${args})`), it.label);
  }
  // 연산 칩만으로 만든 식
  const op = Object.fromEntries(FORMULA_PALETTE.find((g) => g.kind === "op").items.map((i) => [i.label, i.insert]));
  assert.equal(compileFormula(`${op["("]}1 ${op["+"]} 2${op[")"]} ${op["×"]} 3 ${op["÷"]} 2 ${op["−"]} 2 ${op["^"]} 2`).evaluate(row), 0.5);
  // 수식에 쓸 수 없는 항목은 거부
  for (const bad of ["업종", "종목명", "코드", "시장"]) {
    assert.throws(() => compileFormula(`${bad} + 1`), /알 수 없는 항목/, bad);
  }
});

test("블록 이름은 줄임말 없이, 수식도 정식 명칭으로 계산된다", async () => {
  const { FORMULA_PALETTE, FORMULA_KEYWORDS } = await import("../public/formula.js");
  const SHORT = ["시총", "고가52", "저가52", "위치52", "배당률", "배당금", "외국인", "주식수", "가격", "abs", "log", "sqrt", "min", "max"];
  for (const g of FORMULA_PALETTE) for (const it of g.items) {
    assert.ok(!SHORT.includes(it.label), `줄임말 블록: ${it.label}`);
    assert.ok(!/^[A-Z]{2,4}$/.test(it.label), `영문 약어만 있는 블록: ${it.label}`);
  }
  assert.equal(compileFormula("1 / 주가수익비율 + 1 / 주가순자산비율").evaluate(row), 0.6);
  assert.equal(compileFormula("52주최고가 - 52주최저가").evaluate(row), 40000);
  assert.equal(compileFormula("최댓값(주가수익비율, 주가순자산비율)").evaluate(row), 10);
  // 입력 사전에 괄호 붙은 표시명은 없다('(' 입력과 겹치지 않게)
  assert.ok(FORMULA_KEYWORDS.every((k) => !k.word.includes("(")));
});

test("수식 ↔ 블록 변환과 표기 통일", async () => {
  const { formulaToBlocks, blocksToFormula, normalizeFormula } = await import("../public/formula.js");
  const blocks = formulaToBlocks("(가격 - 저가52) / (고가52 - 저가52) * 100");
  assert.deepEqual(blocks.map((b) => b.label),
    ["(", "현재가", "−", "52주 최저가", ")", "÷", "(", "52주 최고가", "−", "52주 최저가", ")", "×", "100"]);
  assert.equal(normalizeFormula("1/PER + 1/PBR"), "1 / 주가수익비율 + 1 / 주가순자산비율");
  assert.equal(normalizeFormula("등락률 * log(거래대금)"), "등락률 * 자연로그(거래대금)");
  // 같은 뜻이면 같은 표기 → 저장된 옛 수식(줄임말)과 새 수식이 같은 것으로 인식된다
  assert.equal(normalizeFormula("순이익/시총*100"), normalizeFormula("순이익 / 시가총액 * 100"));
  // 쓸 수 없는 단어는 글자로 남는다(편집기에서 오류로 보임)
  assert.deepEqual(formulaToBlocks("업종 + 1").map((b) => b.kind), ["text", "op", "num"]);
  assert.equal(normalizeFormula("업종 + 1"), "업종 + 1");
  // 정규화한 수식도 원래와 같은 값을 낸다
  for (const src of ["1/PER + 1/PBR", "(가격 - 저가52) / (고가52 - 저가52) * 100", "max(PER, PBR) ^ 2"]) {
    assert.equal(compileFormula(normalizeFormula(src)).evaluate(row), compileFormula(src).evaluate(row), src);
  }
  assert.equal(blocksToFormula(formulaToBlocks("자연로그(거래대금)")), "자연로그(거래대금)");
});
