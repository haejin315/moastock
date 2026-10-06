// 채팅 제한값과 입력 검사 (순수 함수 - tests/chat.test.mjs 에서 검사)
// 제한값은 Cloudflare 무료 플랜(Durable Objects: 하루 10만 요청, 저장 5GB) 안에서
// 넉넉히 돌아가도록 정했다. WebSocket 메시지는 20개가 요청 1건으로 계산된다.

export const LIMITS = {
  maxRooms: 20,              // 동시에 열려 있는 방
  maxRoomsPerCreator: 2,     // 한 사람(브라우저 ID·IP 기준)이 만들 수 있는 방
  defaultCapacity: 20,
  minCapacity: 2,
  maxCapacity: 30,
  titleMax: 30,
  nickMin: 2,
  nickMax: 12,
  passwordMin: 4,
  passwordMax: 20,
  messageMax: 300,
  history: 100,              // 방마다 보관하는 최근 메시지
  rateCount: 5,              // 5초에 5개까지
  rateWindowMs: 5000,
  idleDeleteMs: 6 * 3600 * 1000,   // 빈 방은 6시간 뒤 삭제 (기본 방 제외)
};

const CONTROL = /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F​-‏‪-‮⁦-⁩]/g;

/** 한 줄 문자열 정리: 제어문자·방향 제어 문자 제거, 공백 정리 */
export function cleanLine(s) {
  return String(s ?? "").replace(CONTROL, "").replace(/\s+/g, " ").trim();
}

export function checkTitle(title) {
  const t = cleanLine(title);
  if (!t) return { error: "방 제목을 입력하세요" };
  if ([...t].length > LIMITS.titleMax) return { error: `방 제목은 ${LIMITS.titleMax}자까지입니다` };
  return { value: t };
}

export function checkNick(nick) {
  const n = cleanLine(nick);
  const len = [...n].length;
  if (len < LIMITS.nickMin || len > LIMITS.nickMax) {
    return { error: `닉네임은 ${LIMITS.nickMin}~${LIMITS.nickMax}자입니다` };
  }
  if (/^(시스템|관리자|운영자|admin|system)$/i.test(n)) return { error: "쓸 수 없는 닉네임입니다" };
  return { value: n };
}

export function checkCapacity(cap) {
  const c = Number(cap ?? LIMITS.defaultCapacity);
  if (!Number.isInteger(c) || c < LIMITS.minCapacity || c > LIMITS.maxCapacity) {
    return { error: `정원은 ${LIMITS.minCapacity}~${LIMITS.maxCapacity}명입니다` };
  }
  return { value: c };
}

/** 비밀번호: 빈 값이면 공개방(null) */
export function checkPassword(pw) {
  if (pw === undefined || pw === null || pw === "") return { value: null };
  const p = String(pw);
  if (p.length < LIMITS.passwordMin || p.length > LIMITS.passwordMax) {
    return { error: `비밀번호는 ${LIMITS.passwordMin}~${LIMITS.passwordMax}자입니다` };
  }
  return { value: p };
}

/** 메시지: 줄바꿈은 살리되 3줄 넘는 빈 줄은 줄인다 */
export function checkMessage(text) {
  const t = String(text ?? "").replace(CONTROL, "").replace(/\r\n?/g, "\n")
    .replace(/[ \t]+/g, " ").replace(/\n{3,}/g, "\n\n").trim();
  if (!t) return { error: "빈 메시지" };
  if ([...t].length > LIMITS.messageMax) return { error: `메시지는 ${LIMITS.messageMax}자까지입니다` };
  return { value: t };
}

/** 슬라이딩 윈도우 속도 제한. times: 최근 전송 시각 배열(그대로 갱신해 돌려준다) */
export function rateAllow(times, now, count = LIMITS.rateCount, windowMs = LIMITS.rateWindowMs) {
  const recent = (times || []).filter((t) => now - t < windowMs);
  if (recent.length >= count) return { ok: false, times: recent, retryMs: windowMs - (now - recent[0]) };
  recent.push(now);
  return { ok: true, times: recent };
}

/** 방 안에서 닉네임이 겹치면 (2), (3)… 을 붙인다 */
export function uniqueNick(nick, taken) {
  if (!taken.includes(nick)) return nick;
  for (let i = 2; ; i++) {
    const n = `${nick}(${i})`;
    if (!taken.includes(n)) return n;
  }
}

export const isRoomId = (id) => typeof id === "string" && /^[a-z0-9]{6,16}$/.test(id);
