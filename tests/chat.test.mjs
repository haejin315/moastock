// 채팅 입력 검사·제한 규칙 (chat/src/rules.js)
import test from "node:test";
import assert from "node:assert/strict";
import { LIMITS, checkCapacity, checkMessage, checkNick, checkPassword, checkTitle, cleanLine,
         isRoomId, rateAllow, uniqueNick } from "../chat/src/rules.js";

test("제한값은 무료 플랜 안에서 보수적으로", () => {
  assert.ok(LIMITS.maxRooms <= 20 && LIMITS.maxCapacity <= 30 && LIMITS.history <= 100);
  assert.ok(LIMITS.rateCount / (LIMITS.rateWindowMs / 1000) <= 1, "초당 1개 이하");
});

test("방 제목: 공백 정리, 길이 제한, 제어·방향 문자 제거", () => {
  assert.equal(checkTitle("  삼성전자   토론 ").value, "삼성전자 토론");
  assert.ok(checkTitle("   ").error);
  assert.ok(checkTitle("가".repeat(LIMITS.titleMax + 1)).error);
  assert.equal(checkTitle("가".repeat(LIMITS.titleMax)).value.length, LIMITS.titleMax);
  assert.equal(cleanLine("a\u202Eb\u0007c\u200Bd"), "abcd");     // 글자 방향 뒤집기·제로폭 문자 차단
});

test("닉네임: 길이, 사칭 금지어", () => {
  assert.equal(checkNick(" 개미1 ").value, "개미1");
  assert.ok(checkNick("a").error);
  assert.ok(checkNick("가".repeat(13)).error);
  for (const n of ["관리자", "시스템", "Admin"]) assert.ok(checkNick(n).error, n);
});

test("정원·비밀번호", () => {
  assert.equal(checkCapacity(undefined).value, LIMITS.defaultCapacity);
  assert.ok(checkCapacity(1).error);
  assert.ok(checkCapacity(LIMITS.maxCapacity + 1).error);
  assert.ok(checkCapacity(2.5).error);
  assert.equal(checkPassword("").value, null);          // 빈 값 = 공개방
  assert.ok(checkPassword("abc").error);
  assert.equal(checkPassword("abcd").value, "abcd");
});

test("메시지: 줄바꿈 유지, 빈 줄 압축, 길이 제한", () => {
  assert.equal(checkMessage(" 안녕\r\n\n\n\n반가워 ").value, "안녕\n\n반가워");
  assert.ok(checkMessage("   \n ").error);
  assert.ok(checkMessage("가".repeat(LIMITS.messageMax + 1)).error);
});

test("속도 제한: 5초에 5개", () => {
  let times = [];
  for (let i = 0; i < 5; i++) {
    const r = rateAllow(times, 1000 + i * 100);
    assert.ok(r.ok); times = r.times;
  }
  const blocked = rateAllow(times, 1600);
  assert.ok(!blocked.ok && blocked.retryMs > 0);
  assert.ok(rateAllow(blocked.times, 1000 + 5000 + 1).ok, "창이 지나면 다시 허용");
});

test("닉네임 중복은 번호를 붙인다, 방 ID 형식", () => {
  assert.equal(uniqueNick("개미", []), "개미");
  assert.equal(uniqueNick("개미", ["개미", "개미(2)"]), "개미(3)");
  assert.ok(isRoomId("lobbyall01") && isRoomId("a1b2c3d4e5"));
  assert.ok(!isRoomId("../lobby") && !isRoomId("ABC123") && !isRoomId("abc"));
});
