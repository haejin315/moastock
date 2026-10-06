# moastock RAG 데이터 파이프라인

종목 관련 **뉴스 원문**과 **DART 공시 원문**을 수집 → 정제 → 중복 제거 → 청킹 → 메타데이터 부착 →
임베딩한 뒤, 실서비스용 벡터 저장소 3종(**pgvector/PostgreSQL · Qdrant · OpenSearch**)에 적재하고
성능을 비교한다. 지금은 전부 로컬에서 돌고, 접속 정보만 바꾸면 클라우드로 옮길 수 있게 만들었다.

결과 수치는 [`REPORT.md`](REPORT.md) (`python -m moarag.report`로 생성).

## 출처 보존 (할루시네이션 방지)

답변 근거를 항상 원문으로 되짚을 수 있게, 모든 청크에 다음을 붙인다.

| 필드 | 뉴스 | 공시 |
|---|---|---|
| `url` | 수집한 네이버 기사 페이지 | DART 뷰어 (`dsaf001/main.do?rcpNo=`) |
| `original_url` | 언론사 원문 링크 | - |
| `publisher` / `author` | 언론사 / 기자 | 제출인 |
| `published_at` | 기사 입력 시각(KST) | 접수일 |
| `char_start`·`char_end` | 정제 전문(`doc.body`) 안의 위치 → 인용 문장 검증 | 〃 |
| `stock_codes` + `stock_match` | 종목과 매핑 방식(`naver_tag`·`code`·`title`·`body`) | 제출 회사(`dart_filer`) |
| `dup_sources` (문서) | 중복 제거로 합쳐진 다른 언론사·URL | - |
| `crawled_at`, `rcept_no`, `report_nm` | 수집 시각 | 접수번호·보고서명 |

pgvector에는 정제 전문(`doc.body`)까지 저장해, 청크 오프셋으로 인용 구간을 그대로 꺼낼 수 있다.

## 파이프라인

```
collect_news_section ─┐  날짜별 네이버 뉴스 섹션 목록(증권 전체 + 산업/재계 중 상장사 언급)
collect_news_index ───┤  종목별 뉴스 목록 → 네이버 종목 태그 (종목당 최근 2,000건 상한)
crawl_news ───────────┤  기사 원문 (본문·언론사·기자·입력/수정 시각·원문 링크·섹션)
collect_dart list/docs┘  OpenDART 목록 + 원문 ZIP → 텍스트 (표는 "셀 | 셀")
        │ raw.sqlite (상태 컬럼으로 이어받기)
process   정제 → 정확/유사(MinHash) 중복 제거 → 문장 단위 토큰 청킹(320/48) → 메타데이터
        │ docs.parquet, chunks.parquet
embed     multilingual-e5-small (384d, CPU) → embeddings.f32.npy
load      pgvector | qdrant | opensearch   (같은 HNSW m=16, ef_construction=128, 코사인)
bench     지연시간 p50/95/99 · recall@10(전수 탐색 대비) · 필터 검색 · 동시성 QPS
report    REPORT.md
```

모든 단계는 시작·종료 시각, 처리 건수, 초당 처리량을 `metrics.jsonl`에 남긴다.

## 실행

```bash
# 1) 환경 (Windows: Git Bash 기준)
py -3.11 -m venv D:/moastock-rag/.venv
D:/moastock-rag/.venv/Scripts/python -m pip install --index-url https://download.pytorch.org/whl/cpu torch
D:/moastock-rag/.venv/Scripts/python -m pip install -r rag/requirements.txt
cp rag/.env.example rag/.env      # DART_API_KEY 입력

# 2) 저장소
docker compose -f rag/docker-compose.yml up -d --build

# 3) 파이프라인 (rag/ 에서)
PY=D:/moastock-rag/.venv/Scripts/python
$PY -m moarag.collect_news_section
$PY -m moarag.collect_news_index
$PY -m moarag.crawl_news
$PY -m moarag.collect_dart list
$PY -m moarag.collect_dart docs        # 일일 한도(2만 건) 초과 시 멈춤 → 다음 날 재실행
$PY -m moarag.process
$PY -m moarag.embed
$PY -m moarag.load all
$PY -m moarag.bench
$PY -m moarag.report
```

## 수집 범위와 한계

- 기간: 실행일 기준 과거 183일 (`MOARAG_WINDOW_DAYS`).
- 뉴스: 네이버 종목뉴스 API는 **종목당 최근 100페이지(2,000건)** 까지만 준다(삼성전자는 약 3일치).
  그래서 기간 전체는 날짜별 섹션 목록(경제>증권 전체, 경제>산업/재계 중 상장사명이 보이는 기사)으로 모으고,
  종목 연결은 네이버 태그가 있으면 그것을, 없으면 회사명 사전 매칭(`stockmatch.py`)을 쓴다.
  2글자 이하 회사명(SK, LG, 한화, 대상 …)은 그룹명·일반명사와 겹쳐 제목에 나올 때만 인정한다.
- 뉴스 원문은 수집 목적(로컬 검색 실험) 안에서만 쓰고 재배포하지 않는다. 서비스 공개 전 언론사·네이버
  이용약관과 저작권 검토가 필요하다.
- 공시: 코스피·코스닥 상장사. 증권 발행 서류(투자설명서·일괄신고추가서류·증권신고서·증권발행실적보고서)는
  대부분 ELS/DLS·채권 정형문이라 제외(`status='excluded'`로 기록).
- 스포츠·연예 등 본문 구조가 다른 기사는 `status='empty'`로 남는다.

## 클라우드 이전

- 접속 정보는 `rag/.env`의 `MOARAG_PG_DSN`·`MOARAG_QDRANT_URL`·`MOARAG_OPENSEARCH_URL`만 바꾼다.
- 이미지 버전은 `docker-compose.yml`에 고정(pgvector 0.8.1/PG18, Qdrant 1.19.1, OpenSearch 3.3.0+nori).
  관리형 서비스로 갈 때 같은 메이저 버전을 고르면 벤치 결과를 그대로 비교할 수 있다.
- 로컬 compose는 보안 플러그인을 끈 상태다. 클라우드에서는 인증·TLS 필수.
- 재적재 없이 옮기려면 `chunks.parquet` + `embeddings.f32.npy` + `docs.parquet`만 가져가 `load`를 다시 돌리면 된다
  (임베딩 재계산 불필요).
