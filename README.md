# 모아스톡 (moastock)

국내외 지수·주식 시세, DART 공시, 증권 뉴스를 한 화면에 모아보는 대시보드.
Cloudflare Pages(정적) + Pages Functions(서버리스 API 프록시)로 동작합니다.

## 기능

- **대시보드** (`/`) — 지수 티커, 관심종목 시세+스파크라인, DART 공시, 뉴스
- **스크리너** (`/screener.html`) — **국내 전 종목(약 2,800개)** 을
  - 주가·등락률·거래량·거래대금·시총·PER·PBR·ROE·배당수익률·외국인비율 등 모든 컬럼으로 정렬
  - **사용자 임의 수식**으로 랭킹: `1/PER + 1/PBR + 배당/100`, `등락률 * log(거래대금)`,
    `(가격-저가52)/(고가52-저가52)` — eval 없는 자체 파서(`public/formula.js`), 한/영 필드명 지원
  - 유니버스: 전체 / 코스피 / 코스닥 / 79개 업종 다중선택 / 내 관심종목 / 검색
  - 값 없는 종목(적자 PER 등)은 정렬 시 자동으로 뒤로

## 데이터 아키텍처 (2계층)

| 계층 | 내용 | 갱신 |
|---|---|---|
| 일일 스냅샷 | 전 종목 펀더멘털 `public/data/snapshot.json` (~800KB) | GitHub Actions 평일 16:40 KST 크론 → `scripts/build_snapshot.py` → 커밋 → CF 자동 배포 |
| 장중 실시간 | 시세/등락/거래량/거래대금/시총 `/api/screener` | 엣지 캐시 60초, 클라이언트가 종목코드로 병합 (PER/PBR은 현재가로 재계산) |

## 구조

```
public/            # 정적 프론트엔드 (프레임워크·빌드 없음)
  screener.html/js # 스크리너 - 필터·정렬·수식 전부 클라이언트에서
  formula.js       # 수식 파서 (재귀 하강, eval 미사용) - tests/formula.test.mjs
  data/snapshot.json
functions/api/
  quote.js         # 시세+스파크라인 (Yahoo Finance v8 chart 프록시, 30s 엣지 캐시)
  screener.js      # 전 종목 실시간 시세 (네이버증권 프록시, 60s 캐시)
  news.js          # 언론사 RSS → JSON (허용 목록 방식, 5min 캐시)
  dart.js          # DART 최신 공시 (API 키는 서버 환경변수, 3min 캐시)
  _utils.js        # 응답/캐시/업스트림 공통
scripts/build_snapshot.py    # 스냅샷 생성기 (.github/workflows/snapshot.yml 크론)
```

브라우저는 같은 도메인의 `/api/*`만 호출합니다 — 원본 API의 CORS 제약을 피하고,
DART API 키가 클라이언트로 내려가지 않으며, 엣지 캐시로 원본 호출량을 줄입니다.
뉴스 프록시는 피드 허용 목록으로 제한해 SSRF를 차단합니다.

## 로컬 개발

```bash
npx wrangler pages dev public
# DART 공시까지 보려면: .dev.vars 파일에 DART_API_KEY=... 추가
```

## 배포 (Cloudflare Pages)

1. Cloudflare 대시보드 → Workers & Pages → Create → Pages → Connect to Git → 이 저장소 선택
2. Build command: (비움) / Build output directory: `public`
3. Settings → Environment variables → `DART_API_KEY` (Secret) 추가 — opendart.fss.or.kr 무료 발급
4. 이후 main 푸시마다 자동 배포

또는 CLI: `npx wrangler pages deploy public`

## 데이터 출처와 고지

- 시세: Yahoo Finance (지연 시세일 수 있음) · 공시: 금융감독원 DART · 뉴스: 각 언론사 공개 RSS
- 본 페이지는 정보 제공 목적이며 투자 판단의 근거가 될 수 없습니다.

## 로드맵

- [ ] 종목 상세(기간별 차트, 재무 요약)
- [ ] 공시 필터(회사/유형) 및 관심종목 연동
- [ ] 공시 Q&A 모듈 연동 (인용·수치 검증 게이트, [dart-disclosure-qa](https://github.com/haejin315/dart-disclosure-qa))
- [ ] 환율·원자재 별도 섹션, 장 마감 요약
