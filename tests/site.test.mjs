// 정적 사이트 점검: 배포 전에 깨진 참조·문법 오류·비밀값 유출을 잡는다.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const PUB = join(ROOT, "public");
const htmlFiles = readdirSync(PUB).filter((f) => f.endsWith(".html"));

test("HTML이 참조하는 로컬 파일(script/css/link)이 모두 존재", () => {
  for (const f of htmlFiles) {
    const html = readFileSync(join(PUB, f), "utf8");
    for (const [, ref] of html.matchAll(/\s(?:src|href)="([^"#?]+)/g)) {
      if (/^(https?:|data:|mailto:|javascript:)/.test(ref)) continue;
      const path = ref.startsWith("/") ? ref.slice(1) : ref;
      if (path === "") continue;
      const target = join(PUB, path);
      // /stock 처럼 확장자 없는 경로는 Pages가 .html로 매핑한다
      assert.ok(existsSync(target) || existsSync(target + ".html"), `${f}: 없는 파일 참조 ${ref}`);
    }
  }
});

test("프런트 스크립트·Functions 문법 오류 없음", () => {
  const jsUnder = (dir) => readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? jsUnder(join(dir, e.name)) : e.name.endsWith(".js") ? [join(dir, e.name)] : []);
  const files = [
    ...readdirSync(PUB).filter((x) => x.endsWith(".js")).map((x) => join(PUB, x)),
    ...jsUnder(join(ROOT, "functions")),
    ...jsUnder(join(ROOT, "chat", "src")),
  ];
  for (const f of files) {
    const r = spawnSync(process.execPath, ["--check", f], { encoding: "utf8" });
    assert.equal(r.status, 0, `${f} 문법 오류
${r.stderr}`);
  }
});

test("모든 페이지에 제목·viewport·면책 고지가 있다", () => {
  for (const f of htmlFiles) {
    const html = readFileSync(join(PUB, f), "utf8");
    assert.match(html, /<title>[^<]+<\/title>/, `${f}: title 없음`);
    assert.match(html, /name="viewport"/, `${f}: viewport 없음`);
    assert.match(html, /투자 판단의 근거가 될 수 없습니다/, `${f}: 면책 고지 없음`);
  }
});

test("배포 파일에 비밀값이 섞이지 않았다", () => {
  const walk = (dir) => readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? walk(join(dir, e.name)) : [join(dir, e.name)]);
  const files = [...walk(PUB), ...walk(join(ROOT, "functions"))].filter((p) => !p.endsWith("snapshot.json"));
  for (const p of files) {
    const text = readFileSync(p, "utf8");
    assert.doesNotMatch(text, /crtfc_key=[0-9a-f]{40}/i, `${p}: DART 키로 보이는 값`);
    assert.doesNotMatch(text, /\b[0-9a-f]{40}\b/, `${p}: 40자리 16진수(키 의심)`);
  }
});

test("스냅샷 데이터 형식", () => {
  const snap = JSON.parse(readFileSync(join(PUB, "data", "snapshot.json"), "utf8"));
  assert.ok(snap.count > 2000 && snap.stocks.length === snap.count, `종목 수 ${snap.count}`);
  for (const s of snap.stocks.slice(0, 50)) {
    assert.match(s.code, /^\d{6}$/);
    assert.ok(["KOSPI", "KOSDAQ"].includes(s.market));
  }
});
