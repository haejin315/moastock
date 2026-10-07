# 모아스톡 AI 비서 (오케스트레이터)

수집한 뉴스·공시(`rag/`)를 근거로 답하는 AI 비서. **흐름은 코드(LangGraph)가 쥐고**, 언어 모델은
"질문 해석"과 "근거로 문장 쓰기"만 한다. 모델은 오픈소스(Qwen3.5 4B)를 직접 서빙한다.

```
사이트 채팅 창 "AI 비서" 탭
  └─ POST /api/assistant (Worker: 하루 질문 한도, 스트림 중계)
       └─ 오케스트레이터 POST /v1/ask (이 폴더, FastAPI + LangGraph, SSE)
            ├─ 언어 모델: OpenAI 호환 API — 로컬 llama.cpp / 운영 vLLM (Qwen3.5 4B)
            ├─ 질문 임베딩: multilingual-e5-small (rag/와 같은 모델)
            └─ 근거 검색: pgvector (rag/ 적재본, 종목·기간 필터)
```

## 흐름 (graph.py)

| 단계 | 하는 일 | 모델 사용 |
|---|---|---|
| understand | 종목(사전 매칭)·기간(규칙) 추출, 질문 종류·매매권유 여부·검색어(JSON) | 짧게 1회 |
| route | 매수·매도 판단 요청 → 거절, 주식과 무관 → 안내, 나머지 → 검색 | – |
| retrieve | 종목·기간·자료 종류로 걸러 검색, 결과가 적으면 조건을 하나씩 풀어 재검색 | – |
| answer | 근거에 [번호]를 달아 넣고 근거만으로 답변(스트리밍) | 1회 |
| verify | 인용 번호가 실제 근거인지, 답변의 숫자가 근거 원문에 있는지 검사 | – |

매매 권유 여부는 모델 판단과 규칙(“사도 될까”, “손절” 등)을 함께 쓴다 — 작은 모델이 놓쳐도 규칙이 잡는다.

## 실행 (로컬)

```bash
# 1) 언어 모델 서버 (llama.cpp, CPU)
llama-server -m Qwen3.5-4B-Q4_K_M.gguf --alias qwen3.5-4b --port 8090 -c 16384 -np 2 --jinja
# 2) 근거 저장소: rag/ 의 postgres 컨테이너 (docker compose -f rag/docker-compose.yml up -d postgres)
# 3) 오케스트레이터
cd assistant && python -m uvicorn server:app --port 8100
# 4) 사이트: .dev.vars 에 ASSIST_URL=http://127.0.0.1:8100 → npx wrangler dev
```

환경변수: `ASSIST_LLM_BASE_URL`, `ASSIST_LLM_MODEL`, `ASSIST_PG_DSN`, `ASSIST_SHARED_SECRET`
(운영에서는 Worker의 `ASSIST_URL`·`ASSIST_SECRET` 비밀값과 맞춘다).

## 평가

```bash
python eval/make_questions.py     # 실제 공시에서 질문 생성 (정답 = 그 공시) → questions.jsonl
python eval/run_eval.py --tag 이름 # 경로 정확도·정답 근거 적중·인용·숫자 근거율·지연시간
```

모델·프롬프트·검색을 바꿀 때마다 같은 질문으로 점수를 비교한다. 결과는 `eval/results/`.
