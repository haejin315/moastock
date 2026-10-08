"""AI 비서 설정. 값은 환경변수로 덮어쓴다 - 로컬(llama.cpp)과 클라우드(vLLM)는 주소만 다르다."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag"))          # moarag(종목 사전·임베딩) 재사용

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 언어 모델: OpenAI 호환 API (로컬 llama-server / 클라우드 vLLM)
LLM_BASE_URL = os.environ.get("ASSIST_LLM_BASE_URL", "http://127.0.0.1:8090/v1")
LLM_MODEL = os.environ.get("ASSIST_LLM_MODEL", "qwen3.5-4b")
LLM_API_KEY = os.environ.get("ASSIST_LLM_API_KEY", "local")

# 근거 저장소 (rag/ 의 pgvector)
PG_DSN = os.environ.get("ASSIST_PG_DSN", "postgresql://moarag:moarag@127.0.0.1:5432/moarag")

# 검색·답변 한도 - 작은 모델(4B)의 문맥 길이와 속도에 맞춘 값
SEARCH_CANDIDATES = 40        # 벡터 검색 후보
KEYWORD_CANDIDATES = 10       # 핵심어 검색 후보 (종목이 정해진 질문만)
RERANK_URL = os.environ.get("ASSIST_RERANK_URL", "")   # 교차 인코더 리랭커(TEI /rerank). 없으면 벡터 순서
EVIDENCE_MAX = 6              # 답변에 넣는 근거 청크 수
EVIDENCE_CHARS = 700          # 근거 하나당 최대 글자
TABLE_MAX = 2                 # 근거 중 공시 표 청크는 최대 N개 (숫자 표가 문단 근거를 밀어내지 않게)
TABLE_CHARS = 1000            # 표 청크는 행 단위로 이 글자 수까지
DEFAULT_DAYS = 120            # 기간을 말하지 않으면 최근 N일
ANSWER_MAX_TOKENS = 700

DISCLAIMER = ("이 답변은 수집된 뉴스·공시를 근거로 한 정보 제공이며 투자 권유가 아닙니다. "
              "투자 판단과 그 결과의 책임은 본인에게 있습니다.")
