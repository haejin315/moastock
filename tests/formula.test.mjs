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
