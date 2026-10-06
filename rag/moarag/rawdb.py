"""원천(raw) 저장소: SQLite 하나. 수집 단계는 모두 여기에 쓰고, 중단 후 재실행하면
이어서 진행한다(status 컬럼 기반).

출처 추적(할루시네이션 방지)을 위해 원문 URL·언론사 원문 링크·수집 시각·HTTP 상태를
문서마다 그대로 남긴다.
"""
from __future__ import annotations

import sqlite3

from .config import RAW_DB

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS stock (
  code TEXT PRIMARY KEY, name TEXT, market TEXT,
  news_indexed_at TEXT,          -- 뉴스 목록 수집 완료 시각 (재실행 시 건너뜀)
  news_links INTEGER
);

-- 종목 ↔ 기사. method: naver_tag(네이버 종목뉴스 목록) | code | title | body (stockmatch 사전 매칭)
CREATE TABLE IF NOT EXISTS news_link (
  code TEXT, oid TEXT, aid TEXT, listed_at TEXT, method TEXT DEFAULT 'naver_tag',
  PRIMARY KEY (code, oid, aid)
);

-- 섹션 목록 수집 진행 (날짜×섹션 단위로 완료 표시)
CREATE TABLE IF NOT EXISTS section_day (
  sid2 TEXT, date TEXT, pages INTEGER, listed INTEGER, kept INTEGER, done_at TEXT,
  PRIMARY KEY (sid2, date)
);

CREATE TABLE IF NOT EXISTS news (
  oid TEXT, aid TEXT,
  naver_url TEXT,                -- 수집한 페이지 (출처)
  list_source TEXT,              -- stock_api | section:258 | section:261 ...
  list_title TEXT, list_lede TEXT, list_office TEXT, list_datetime TEXT,
  status TEXT DEFAULT 'pending', -- pending | ok | failed | empty
  http_status INTEGER, final_url TEXT, error TEXT, fetched_at TEXT,
  title TEXT, body TEXT, press TEXT, reporter TEXT,
  published_at TEXT, modified_at TEXT, section TEXT,
  original_url TEXT,             -- 언론사 원문 링크 (출처)
  PRIMARY KEY (oid, aid)
);
CREATE INDEX IF NOT EXISTS news_status ON news(status);

CREATE TABLE IF NOT EXISTS dart_filing (
  rcept_no TEXT PRIMARY KEY,
  corp_code TEXT, corp_name TEXT, stock_code TEXT, corp_cls TEXT,
  report_nm TEXT, flr_nm TEXT, rcept_dt TEXT, rm TEXT,
  status TEXT DEFAULT 'pending', -- pending | ok | failed | empty | excluded(증권발행 서류)
  error TEXT, fetched_at TEXT,
  doc_files INTEGER, doc_bytes INTEGER,
  body TEXT
);
CREATE INDEX IF NOT EXISTS dart_status ON dart_filing(status);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(RAW_DB, timeout=60)
    con.executescript(SCHEMA)
    return con


def kv_get(con, k, default=None):
    row = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def kv_set(con, k, v):
    con.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    con.commit()
