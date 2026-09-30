# 모아스톡 (moastock)

국내외 지수·주식 시세, DART 공시, 증권 뉴스를 한 화면에 모아보는 대시보드.
Cloudflare Pages(정적) + Pages Functions(서버리스 API 프록시)로 동작합니다.

## 구조

```
public/            # 정적 프론트엔드 (프레임워크·빌드 없음)
functions/api/
  quote.js         # 시세+스파크라인 (Yahoo Finance v8 chart 프록시, 30s 엣지 캐시)
  news.js          # 언론사 RSS → JSON (허용 목록 방식, 5min 캐시)
  dart.js          # DART 최신 공시 (API 키는 서버 환경변수, 3min 캐시)
  _utils.js        # 응답/캐시/업스트림 공통
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
