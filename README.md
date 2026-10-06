# 모아스톡 (moastock)

국내외 지수·주식 시세, DART 공시, 증권 뉴스를 한 화면에 모아보는 대시보드.
Cloudflare Worker 하나(정적 페이지 + 서버리스 API 프록시 + 실시간 채팅 Durable Objects)로 동작합니다.

## 기능

- **대시보드** (`/`) — 지수 티커, 관심종목 시세+스파크라인, DART 공시, 뉴스
- **스크리너** (`/screener.html`) — **국내 전 종목(약 2,800개)** 을
  - 주가·등락률·거래량·거래대금·시총·PER·PBR·ROE·배당수익률·외국인비율 등 모든 컬럼으로 정렬
  - **사용자 임의 수식**으로 랭킹: `1 / 주가수익비율 + 1 / 주가순자산비율`, `등락률 * 자연로그(거래대금)`,
    `(현재가 - 52주최저가) / (52주최고가 - 52주최저가)` — eval 없는 자체 파서(`public/formula.js`)
  - **수식 블록 편집기**(`public/formula-editor.js`): 항목·연산·함수 블록을 끌어다 놓거나 클릭해 수식을 만들고,
    직접 입력해도 이름·숫자·기호가 완성되면 블록으로 바뀐다(영문 약어 PER 등도 정식 명칭 블록으로).
    블록이 되지 않은 글자는 수식에 쓸 수 없는 단어 → 즉시 오류 표시. 업종·종목명 같은 문자 항목은 넣을 수 없다
  - 유니버스: 전체 / 코스피 / 코스닥 / 79개 업종 다중선택 / 내 관심종목 / 검색
  - 값 없는 종목(적자 PER 등)은 정렬 시 자동으로 뒤로

- **실시간 채팅** (모든 페이지 오른쪽 아래 💬) — 메신저 형태의 채팅방
  - 방 목록 / 방 만들기(제목·비밀번호·정원) / 방장 설정(제목·비밀번호·정원 변경, 삭제) / 안 읽은 메시지 배지
  - 계정 없이 닉네임 + 브라우저 ID. 방장 권한은 방을 만든 브라우저에 저장된 토큰으로
  - "AI 비서" 탭은 준비 중 (rag/ 데이터로 만들 에이전트 챗봇 자리)

### 채팅 구조와 무료 한도

Cloudflare Durable Objects(SQLite, 무료 플랜 지원) + WebSocket 휴면 API. 방 하나 = DO 하나, 방 목록 = 로비 DO 하나.
DO 클래스(`src/chat/`)는 사이트와 같은 Worker에 있고, `/api/chat/*`가 DO 바인딩으로 연결한다.

| 제한 | 값 | 이유 |
|---|---|---|
| 동시에 열린 방 | 20 | 무료 한도(하루 요청 10만, WebSocket 메시지 20개 = 요청 1건) 안에서 여유 있게 |
| 방 정원 | 기본 20, 최대 30 | |
| 한 사람이 만드는 방 | 2 (브라우저 ID 또는 IP) | 같은 공유기(회사·학교) 사용자는 합쳐서 2개 |
| 메시지 | 300자, 5초에 5개 | 도배 방지 |
| 보관 | 방마다 최근 100개 | |
| 빈 방 정리 | 6시간 | 기본 방 "전체 채팅"은 유지 |


## 데이터 아키텍처 (2계층)

| 계층 | 내용 | 갱신 |
|---|---|---|
| 일일 스냅샷 | 전 종목 펀더멘털 `public/data/snapshot.json` (~800KB) | GitHub Actions 평일 16:40 KST 크론 → `scripts/build_snapshot.py` → 커밋 → Worker 자동 배포 |
| 장중 실시간 | 시세/등락/거래량/거래대금/시총 `/api/screener` | 엣지 캐시 60초, 클라이언트가 종목코드로 병합 (PER/PBR은 현재가로 재계산) |

## 구조

```
wrangler.toml      # Worker 설정: 정적 파일·D1·Durable Objects·도메인 라우트
public/            # 정적 프론트엔드 (프레임워크·빌드 없음) - Workers Static Assets로 바로 서빙
  screener.html/js # 스크리너 - 필터·정렬·수식 전부 클라이언트에서
  formula.js       # 수식 파서 (재귀 하강, eval 미사용) - tests/formula.test.mjs
  formula-editor.js# 수식 블록 편집기
  chat.js          # 오른쪽 아래 채팅 창
  data/snapshot.json
src/
  worker.js        # 진입점: /api/* → router, 나머지 → 정적 파일, DO 클래스 내보내기
  router.js        # /api/<이름> → src/api/<이름>.js (onRequestGet 등) - tests/router.test.mjs
  chat/            # 채팅 Durable Objects: lobby.js(방 목록), room.js(방), rules.js(제한·검사)
  api/
  quote.js         # 시세+스파크라인 (Yahoo Finance v8 chart 프록시, 30s 엣지 캐시)
  screener.js      # 전 종목 실시간 시세 (네이버증권 프록시, 60s 캐시)
  news.js          # 언론사 RSS → JSON (허용 목록 방식, 5min 캐시)
  dart.js          # DART 최신 공시 (API 키는 Worker 비밀값, 3min 캐시)
  board.js         # 종목 토론방 (D1)
  chat.js          # 채팅 프록시 → Durable Objects
  _utils.js        # 응답/캐시/업스트림 공통
scripts/build_snapshot.py    # 스냅샷 생성기 (.github/workflows/snapshot.yml 크론)
```

브라우저는 같은 도메인의 `/api/*`만 호출합니다 — 원본 API의 CORS 제약을 피하고,
DART API 키가 클라이언트로 내려가지 않으며, 엣지 캐시로 원본 호출량을 줄입니다.
뉴스 프록시는 피드 허용 목록으로 제한해 SSRF를 차단합니다.

## 로컬 개발

```bash
npx wrangler dev          # http://127.0.0.1:8787 - 정적 페이지·API·채팅·D1(로컬) 모두
# DART 공시까지 보려면: .dev.vars 파일에 DART_API_KEY=... 추가
# (로컬 런타임은 OpenDART의 TLS 방식(RSA 키교환)을 막아 DART만 로컬에서 실패할 수 있다. 운영은 정상)
```

## 테스트와 배포 흐름 (CI/CD)

```bash
npm test     # node --test "tests/*.test.mjs" - 외부 의존성 없음
```

| 테스트 | 내용 |
|---|---|
| `tests/formula.test.mjs` | 스크리너 수식 파서 |
| `tests/api.test.mjs` | API(dart·quote·stockfeed·news·screener·chart) - fetch·엣지 캐시를 가짜로 바꿔 네트워크 없이 검증. API 키가 응답에 새지 않는지 포함 |
| `tests/router.test.mjs` | `/api/*` 분기, 404·405, 예외 시 500(내부 메시지 비노출), 채팅 → 로비 DO 전달 |
| `tests/chat.test.mjs` | 채팅 제한·입력 검사(제목·닉네임·비밀번호·메시지·속도 제한) |
| `tests/site.test.mjs` | HTML이 참조하는 파일 존재, JS 문법(`node --check`), 면책 고지, 비밀값 유출, 스냅샷 형식 |

Workers Builds(GitHub 연동)의 **빌드 명령이 `npm test`**, 배포 명령이 `npx wrangler deploy`다.
**테스트가 하나라도 실패하면 배포되지 않는다**(기존 버전 유지). Node 버전은 `.node-version`(22).

- `main` 푸시 → 테스트 → 운영 배포 (moastock.co.kr, www.moastock.co.kr)

## 배포 (Cloudflare Worker)

- 자동(연결됨): Workers Builds ← GitHub `haejin315/moastock` main. Build `npm test` → Deploy `npx wrangler deploy`
- 수동: `npx wrangler deploy`
- 비밀값: `npx wrangler secret put DART_API_KEY` — opendart.fss.or.kr 무료 발급
- 도메인: `wrangler.toml`의 `routes`(사용자 지정 도메인 moastock.co.kr·www, DNS·인증서 자동)

## 데이터 출처와 고지

- 대시보드 시세·스파크라인: Yahoo Finance (지연 시세일 수 있음)
- 스크리너 전 종목 시세·재무 지표(PER/PBR 등)·업종: 네이버증권 (지연·전일 기준일 수 있음)
- 공시: 금융감독원 DART 공식 OpenAPI · 뉴스: 각 언론사 공개 RSS
- 순이익·순자산·주식수·ROE·52주위치는 위 값에서 파생 계산한 추정치
- 본 페이지는 정보 제공 목적이며 투자 판단의 근거가 될 수 없습니다.

## 로드맵

- [ ] 종목 상세(기간별 차트, 재무 요약)
- [ ] 공시 필터(회사/유형) 및 관심종목 연동
- [ ] 공시 Q&A 모듈 연동 (인용·수치 검증 게이트, [dart-disclosure-qa](https://github.com/haejin315/dart-disclosure-qa))
- [ ] 환율·원자재 별도 섹션, 장 마감 요약
