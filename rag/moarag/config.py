"""파이프라인 공통 설정. 값은 환경변수(.env)로 덮어쓴다.

데이터는 저장소 밖(MOARAG_DATA_DIR, 기본 D:/moastock-rag/data)에 둔다 — 원문 수십만 건과
임베딩이 깃에 들어가지 않게.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # rag/
REPO = ROOT.parent                                   # moastock/


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env(ROOT / ".env")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

KST = timezone(timedelta(hours=9))

DATA_DIR = Path(os.environ.get("MOARAG_DATA_DIR", "D:/moastock-rag/data"))
RAW_DB = DATA_DIR / "raw.sqlite"
CHUNKS_PATH = DATA_DIR / "chunks.parquet"
DOCS_PATH = DATA_DIR / "docs.parquet"
EMB_PATH = DATA_DIR / "embeddings.f32.npy"
METRICS_PATH = DATA_DIR / "metrics.jsonl"
BENCH_DIR = DATA_DIR / "bench"
SNAPSHOT = REPO / "public" / "data" / "snapshot.json"

# 수집 기간: 오늘(KST) 기준 과거 N일
WINDOW_DAYS = int(os.environ.get("MOARAG_WINDOW_DAYS", "183"))
NOW_KST = datetime.now(KST)
SINCE_KST = (NOW_KST - timedelta(days=WINDOW_DAYS)).replace(hour=0, minute=0, second=0, microsecond=0)

DART_API_KEY = os.environ.get("DART_API_KEY", "")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36 moastock-rag/1.0"
)

# 임베딩
EMBED_MODEL = os.environ.get("MOARAG_EMBED_MODEL", "intfloat/multilingual-e5-small")
EMBED_DIM = 384
CHUNK_TOKENS = 320        # e5 최대 512 토큰 - 제목 접두어 여유분을 남긴다
CHUNK_OVERLAP = 48

# 저장소 접속 (docker-compose.yml 기본값). Windows에서 localhost는 IPv6(::1)를 먼저 시도해
# 요청마다 수십 ms가 붙으므로 127.0.0.1로 고정한다.
PG_DSN = os.environ.get("MOARAG_PG_DSN", "postgresql://moarag:moarag@127.0.0.1:5432/moarag")
QDRANT_URL = os.environ.get("MOARAG_QDRANT_URL", "http://127.0.0.1:6333")
OPENSEARCH_URL = os.environ.get("MOARAG_OPENSEARCH_URL", "http://127.0.0.1:9200")

# 세 저장소 공통 HNSW 파라미터 - 비교를 공정하게
HNSW_M = 16
HNSW_EF_CONSTRUCTION = 128
COLLECTION = "moarag_chunks"

DATA_DIR.mkdir(parents=True, exist_ok=True)
