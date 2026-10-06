"""4단계: 정제 → 중복 제거 → 청킹 → 메타데이터 부착.

  python -m moarag.process [--workers 14]

입력: raw.sqlite (news status='ok', dart_filing status='ok')
출력: docs.parquet   - 정제된 문서 전문 (인용 검증용 원문 보관)
      chunks.parquet - 임베딩/적재 대상 청크 + 출처 메타데이터

중복 제거
  1) 정확 중복: 정제 본문 SHA-1 이 같은 문서 (같은 통신사 기사를 여러 매체가 전재 등)
  2) 유사 중복: MinHash LSH (문자 5-gram, Jaccard ≥ 0.85) - 오탈자·꼬리말만 다른 전재 기사
  대표 문서는 가장 먼저 발행된 것. 버려진 문서의 출처(언론사·URL)와 종목 매핑은 대표 문서의
  dup_sources / stock_codes 로 합쳐 출처가 사라지지 않게 한다.
  3) 청크 중복: 동일 텍스트 청크(공시 정형 문구 등)는 첫 번째만 남긴다.

청킹: 문장 단위로 끊고 토크나이저(e5) 토큰 기준 CHUNK_TOKENS 이하로 묶으며, 앞 청크의 마지막
문장들을 CHUNK_OVERLAP 토큰만큼 겹친다. 청크마다 정제 문서 내 문자 오프셋을 남겨 인용 위치를
되짚을 수 있다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import unicodedata
import uuid
from collections import defaultdict
from datetime import datetime
from multiprocessing import Pool

import pyarrow as pa
import pyarrow.parquet as pq

from .config import (CHUNK_OVERLAP, CHUNK_TOKENS, CHUNKS_PATH, DOCS_PATH, EMBED_MODEL, KST,
                     SINCE_KST)
from .metrics import Progress, Stage
from .rawdb import connect
from .stockmatch import StockMatcher

# 종목 매핑 신뢰도 순서 (같은 종목이 여러 방식으로 잡히면 앞의 것을 기록)
MATCH_RANK = {"naver_tag": 0, "code": 1, "title": 2, "body": 3}

PIPELINE_VERSION = "2026-10-01.1"
NS = uuid.UUID("6f1c2a5e-8f0b-4c55-9d7a-0c3c2b7f4e11")

# ---- 정제 ---------------------------------------------------------------------

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
# 짧은 줄에서만 지운다 (본문 문장을 잘못 지우지 않도록)
BOILER = re.compile(
    # ■◆● 등은 본문 소제목에도 쓰이므로 지우지 않는다. ▶☞는 거의 항상 홍보·링크 줄.
    r"(무단\s*전재|재배포\s*금지|copyright|ⓒ|©|all rights reserved|저작권자|"
    r"^\s*[▶☞]|제보|구독\s*(하기|신청)|카카오톡\s*[:：@]|\[카카오톡\]|네이버에서|채널\s*추가|^\s*\[?(메일|이메일)\]?\s*:|"
    r"^\s*\[?\s*(사진|그래픽|자료|영상)\s*[=:]|^\s*관련\s*기사|^\s*<\s*저작권)",
    re.I)
REPORTER_LINE = re.compile(r"^[\w\s·,]{0,30}(기자|특파원|논설위원|객원기자|에디터)\s*$")
WS = re.compile(r"[ \t ​　]+")


def clean_news(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    out = []
    for ln in text.splitlines():
        ln = WS.sub(" ", ln).strip()
        if not ln:
            continue
        if len(ln) < 90 and BOILER.search(ln):
            continue
        ln = EMAIL.sub("", ln).strip()
        if not ln or REPORTER_LINE.match(ln):
            continue
        out.append(ln)
    return "\n".join(out)


def clean_dart(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    out = []
    for ln in text.splitlines():
        ln = WS.sub(" ", ln).strip()
        if not ln or re.fullmatch(r"[-=_|. ]+", ln):
            continue
        out.append(ln)
    return "\n".join(out)


def norm_for_hash(text: str) -> str:
    return re.sub(r"\W+", "", text).lower()


# ---- 유사 중복 (MinHash) -------------------------------------------------------

NUM_PERM = 64


def minhash_of(text: str):
    from datasketch import LeanMinHash, MinHash
    s = norm_for_hash(text)[:3000]
    m = MinHash(num_perm=NUM_PERM)
    m.update_batch([s[i:i + 5].encode() for i in range(max(1, len(s) - 4))])
    return LeanMinHash(m)


# ---- 청킹 ---------------------------------------------------------------------

SENT_SPLIT = re.compile(r"(?<=[.?!。])\s+|(?<=다\.)|(?<=요\.)|\n")

_tok = None


def tokenizer():
    global _tok
    if _tok is None:
        from transformers import AutoTokenizer
        _tok = AutoTokenizer.from_pretrained(EMBED_MODEL)
    return _tok


def split_units(text: str):
    """(시작오프셋, 끝오프셋) 문장 단위 목록."""
    units, pos = [], 0
    for m in SENT_SPLIT.finditer(text):
        if m.start() > pos:
            units.append((pos, m.start()))
        pos = m.end()
    if pos < len(text):
        units.append((pos, len(text)))
    return [(a, b) for a, b in units if text[a:b].strip()]


def chunk_doc(text: str):
    """[(char_start, char_end, n_tokens)] - 토큰 기준 묶기 + 문장 겹침."""
    tok = tokenizer()
    units = split_units(text)
    if not units:
        return []
    lens = [len(x) for x in tok([text[a:b] for a, b in units], add_special_tokens=False)["input_ids"]]
    # 한 문장이 한도를 넘으면 문자 길이 비례로 잘게 쪼갠다
    fixed_units, fixed_lens = [], []
    for (a, b), n in zip(units, lens):
        if n <= CHUNK_TOKENS:
            fixed_units.append((a, b)); fixed_lens.append(n)
            continue
        parts = -(-n // CHUNK_TOKENS)
        step = -(-(b - a) // parts)
        for s in range(a, b, step):
            fixed_units.append((s, min(b, s + step))); fixed_lens.append(-(-n // parts))
    units, lens = fixed_units, fixed_lens

    chunks, i = [], 0
    while i < len(units):
        j, total = i, 0
        while j < len(units) and total + lens[j] <= CHUNK_TOKENS:
            total += lens[j]; j += 1
        if j == i:
            j, total = i + 1, lens[i]
        chunks.append((units[i][0], units[j - 1][1], total))
        if j >= len(units):
            break
        # 겹침: 끝에서부터 CHUNK_OVERLAP 토큰 이하의 문장들을 다음 청크 앞에 다시 포함
        k, back = j, 0
        while k - 1 > i and back + lens[k - 1] <= CHUNK_OVERLAP:
            back += lens[k - 1]; k -= 1
        i = k
    return chunks


def chunk_worker(args):
    doc_id, text = args
    return doc_id, chunk_doc(text)


# ---- 메인 ---------------------------------------------------------------------

WINDOW = 500       # 청킹 병렬 처리 단위(문서 수) - 공시 대용량 문서가 몰려도 메모리가 튀지 않게

DOC_SCHEMA = pa.schema([
    ("doc_id", pa.string()), ("source_type", pa.string()), ("title", pa.string()), ("text", pa.large_string()),
    ("publisher", pa.string()), ("author", pa.string()), ("published_at", pa.string()),
    ("modified_at", pa.string()), ("url", pa.string()), ("original_url", pa.string()), ("section", pa.string()),
    ("stock_codes", pa.list_(pa.string())), ("stock_match", pa.string()), ("rcept_no", pa.string()),
    ("report_nm", pa.string()), ("corp_name", pa.string()), ("crawled_at", pa.string()),
    ("content_sha1", pa.string()), ("dup_sources", pa.string()),
])

CHUNK_SCHEMA = pa.schema([
    ("chunk_id", pa.string()), ("doc_id", pa.string()), ("chunk_index", pa.int32()), ("n_chunks", pa.int32()),
    ("text", pa.string()), ("char_start", pa.int32()), ("char_end", pa.int32()), ("n_tokens", pa.int32()),
    ("chunk_sha1", pa.string()), ("source_type", pa.string()), ("title", pa.string()), ("publisher", pa.string()),
    ("author", pa.string()), ("published_at", pa.string()), ("url", pa.string()), ("original_url", pa.string()),
    ("stock_codes", pa.list_(pa.string())), ("stock_names", pa.list_(pa.string())), ("stock_match", pa.string()),
    ("corp_name", pa.string()), ("report_nm", pa.string()), ("rcept_no", pa.string()), ("section", pa.string()),
    ("n_dup_sources", pa.int32()), ("crawled_at", pa.string()), ("pipeline_version", pa.string()),
])


def doc_row(d: dict) -> dict:
    return {**{k: d.get(k) for k in DOC_SCHEMA.names if k not in ("stock_match", "dup_sources")},
            "stock_match": json.dumps(d["stock_match"], ensure_ascii=False),
            "dup_sources": json.dumps(d["dup_sources"], ensure_ascii=False)}


def kst_iso(s: str | None, fmt: str | None = None) -> str | None:
    if not s:
        return None
    try:
        d = datetime.strptime(s, fmt) if fmt else datetime.fromisoformat(s)
        return d.replace(tzinfo=KST).isoformat()
    except ValueError:
        return None


def main(workers: int):
    con = connect()
    names = dict(con.execute("SELECT code, name FROM stock"))
    since_iso = SINCE_KST.isoformat()
    docs: dict[str, dict] = {}

    # 1) 적재 + 정제
    with Stage("process_clean") as st:
        matcher = StockMatcher()
        links: dict[str, dict[str, str]] = defaultdict(dict)
        for code, oid, aid, how in con.execute("SELECT code, oid, aid, method FROM news_link"):
            links[f"news:{oid}-{aid}"][code] = how
        for (oid, aid, naver_url, title, body, press, reporter, pub, mod, section, orig, fetched,
             list_title, list_office, list_dt) in con.execute(
                "SELECT oid,aid,naver_url,title,body,press,reporter,published_at,modified_at,section,original_url,"
                "fetched_at,list_title,list_office,list_datetime FROM news WHERE status='ok'"):
            st.add(news_in=1, chars_in=len(body))
            published = kst_iso(pub, "%Y-%m-%d %H:%M:%S") or kst_iso(list_dt, "%Y%m%d%H%M")
            if not published or published < since_iso:
                st.add(news_out_of_window=1); continue
            text = clean_news(body)
            if len(text) < 80:
                st.add(news_too_short=1); continue
            doc_id = f"news:{oid}-{aid}"
            how = dict(links.get(doc_id, {}))
            for code, m in matcher.match(title or list_title or "", text).items():
                if code not in how or MATCH_RANK[m] < MATCH_RANK.get(how[code], 9):
                    how[code] = m
            codes = sorted(how)
            docs[doc_id] = dict(
                doc_id=doc_id, source_type="news", title=(title or list_title or "").strip(), text=text,
                publisher=press or list_office, author=reporter, published_at=published,
                modified_at=kst_iso(mod, "%Y-%m-%d %H:%M:%S"), url=naver_url, original_url=orig,
                section=section, stock_codes=codes, stock_match=how, rcept_no=None, report_nm=None, corp_name=None,
                crawled_at=fetched)
            st.add(news_kept=1, chars_out=len(text))

        for (rno, corp_code, corp_name, stock_code, cls, report_nm, flr_nm, rcept_dt, rm, body, fetched) in con.execute(
                "SELECT rcept_no,corp_code,corp_name,stock_code,corp_cls,report_nm,flr_nm,rcept_dt,rm,body,fetched_at "
                "FROM dart_filing WHERE status='ok'"):
            st.add(dart_in=1, chars_in=len(body))
            text = clean_dart(body)
            if len(text) < 80:
                st.add(dart_too_short=1); continue
            doc_id = f"dart:{rno}"
            docs[doc_id] = dict(
                doc_id=doc_id, source_type="dart", title=f"[{corp_name}] {report_nm}", text=text,
                publisher=flr_nm, author=None, published_at=kst_iso(rcept_dt, "%Y%m%d"), modified_at=None,
                url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rno}", original_url=None,
                section=rm or None, stock_codes=[stock_code] if stock_code else [],
                stock_match={stock_code: "dart_filer"} if stock_code else {}, rcept_no=rno,
                report_nm=report_nm, corp_name=corp_name, crawled_at=fetched)
            st.add(dart_kept=1, chars_out=len(text))

    # 2) 중복 제거
    with Stage("process_dedup", docs=len(docs)) as st:
        for d in docs.values():
            d["content_sha1"] = hashlib.sha1(norm_for_hash(d["text"]).encode()).hexdigest()
            d["dup_sources"] = []
        order = sorted(docs.values(), key=lambda d: (d["published_at"] or "", d["doc_id"]))

        def merge(keep, drop, kind):
            for c, m in drop["stock_match"].items():
                if c not in keep["stock_match"] or MATCH_RANK.get(m, 9) < MATCH_RANK.get(keep["stock_match"][c], 9):
                    keep["stock_match"][c] = m
            keep["stock_codes"] = sorted(keep["stock_match"])
            keep["dup_sources"].append({"doc_id": drop["doc_id"], "publisher": drop["publisher"],
                                        "url": drop["url"], "original_url": drop["original_url"],
                                        "published_at": drop["published_at"], "match": kind})
            keep["dup_sources"].extend(drop["dup_sources"])

        first_by_hash: dict[str, dict] = {}
        survivors = []
        for d in order:
            k = (d["source_type"], d["content_sha1"])
            if k in first_by_hash:
                merge(first_by_hash[k], d, "exact"); st.add(exact_dups=1)
            else:
                first_by_hash[k] = d; survivors.append(d)

        # 유사 중복은 뉴스에만 (공시는 정정공시 등 의도된 유사 문서가 많아 정확 중복만)
        from datasketch import MinHashLSH
        news = [d for d in survivors if d["source_type"] == "news"]
        with Pool(workers) as pool:
            sigs = pool.map(minhash_of, [d["text"] for d in news], chunksize=256)
        lsh = MinHashLSH(threshold=0.85, num_perm=NUM_PERM)
        dropped = set()
        for d, m in zip(news, sigs):      # 발행순 - 먼저 나온 문서가 대표
            hits = lsh.query(m)
            if hits:
                merge(docs[hits[0]], d, "near"); dropped.add(d["doc_id"]); st.add(near_dups=1)
            else:
                lsh.insert(d["doc_id"], m)
        survivors = [d for d in survivors if d["doc_id"] not in dropped]
        st.add(docs_out=len(survivors))

    # 3) 문서 저장 → 메모리 해제
    with Stage("process_write_docs") as st:
        del docs, order, first_by_hash, news, sigs, lsh
        w = pq.ParquetWriter(DOCS_PATH, DOC_SCHEMA, compression="zstd")
        for i in range(0, len(survivors), 20_000):
            w.write_table(pa.Table.from_pylist([doc_row(d) for d in survivors[i:i + 20_000]], schema=DOC_SCHEMA))
        w.close()
        st.add(docs=len(survivors), docs_bytes=DOCS_PATH.stat().st_size)

    del survivors
    chunk_stage(workers, names)


def chunk_stage(workers: int, names: dict | None = None):
    """docs.parquet을 WINDOW 문서씩 읽어 청킹한다.
    - 전체 본문을 메모리에 올리지 않으므로 피크 메모리가 창 크기 + 중복 해시 집합 수준으로 묶인다.
      (imap에 전체 제너레이터를 넘기면 피더 스레드가 모든 본문을 미리 큐에 피클링해 수십 GB를 쓴다)
    - 창마다 결과를 parts/part-NNNNN.parquet로 쓰고, 다시 실행하면 끝난 창은 건너뛴다(중단 내성).
      끝나면 parts를 chunks.parquet 하나로 합친다.
    """
    if names is None:
        names = dict(connect().execute("SELECT code, name FROM stock"))
    pf = pq.ParquetFile(DOCS_PATH)
    n_docs = pf.metadata.num_rows
    parts_dir = CHUNKS_PATH.parent / "chunk_parts"
    parts_dir.mkdir(exist_ok=True)
    sig = f"{DOCS_PATH.stat().st_size}-{DOCS_PATH.stat().st_mtime_ns}-{WINDOW}-{CHUNK_TOKENS}-{CHUNK_OVERLAP}"
    sig_file = parts_dir / "SIGNATURE"
    if not sig_file.exists() or sig_file.read_text() != sig:     # 입력이 바뀌었으면 처음부터
        for f in parts_dir.glob("part-*.parquet"):
            f.unlink()
        sig_file.write_text(sig)
    done_parts = sorted(parts_dir.glob("part-*.parquet"))

    # 이미 만든 청크의 해시를 복원해 청크 중복 제거를 이어간다
    seen_chunk: set[bytes] = set()
    for f in done_parts:
        seen_chunk.update(bytes.fromhex(h) for h in pq.read_table(f, columns=["chunk_sha1"]).column(0).to_pylist())

    with Stage("process_chunk", docs=n_docs, chunk_tokens=CHUNK_TOKENS, overlap=CHUNK_OVERLAP,
               workers=workers, resumed_windows=len(done_parts)) as st:
        prog = Progress("청킹", total=n_docs - len(done_parts) * WINDOW)
        with Pool(workers) as pool:
            for wi, rb in enumerate(pf.iter_batches(batch_size=WINDOW)):
                part = parts_dir / f"part-{wi:05d}.parquet"
                if part.exists():
                    continue
                window = rb.to_pylist()
                results = pool.map(chunk_worker, [(d["doc_id"], d["text"]) for d in window], chunksize=8)
                rows = []
                for d, (_, spans) in zip(window, results):
                    kept = []
                    for a, b, ntok in spans:
                        t = d["text"][a:b].strip()
                        h = hashlib.sha1(norm_for_hash(t).encode()).digest()
                        if h in seen_chunk:
                            st.add(chunk_dups=1); continue
                        seen_chunk.add(h)
                        kept.append((a, b, ntok, t, h.hex()))
                    stock_names = [names.get(c, c) for c in (d["stock_codes"] or [])]
                    n_dup = len(json.loads(d["dup_sources"] or "[]"))
                    for i, (a, b, ntok, t, h) in enumerate(kept):
                        rows.append(dict(
                            chunk_id=str(uuid.uuid5(NS, f"{d['doc_id']}#{i}")), doc_id=d["doc_id"], chunk_index=i,
                            n_chunks=len(kept), text=t, char_start=a, char_end=b, n_tokens=ntok, chunk_sha1=h,
                            source_type=d["source_type"], title=d["title"], publisher=d["publisher"],
                            author=d["author"], published_at=d["published_at"], url=d["url"],
                            original_url=d["original_url"], stock_codes=d["stock_codes"], stock_names=stock_names,
                            stock_match=d["stock_match"], corp_name=d["corp_name"], report_nm=d["report_nm"],
                            rcept_no=d["rcept_no"], section=d["section"], n_dup_sources=n_dup,
                            crawled_at=d["crawled_at"], pipeline_version=PIPELINE_VERSION))
                    st.add(chunks=len(kept), tokens=sum(k[2] for k in kept))
                tmp = part.with_suffix(".tmp")
                pq.write_table(pa.Table.from_pylist(rows, schema=CHUNK_SCHEMA), tmp, compression="zstd")
                os.replace(tmp, part)          # 원자적 교체 - 중간에 죽어도 반쪽 파일이 남지 않는다
                prog.tick(len(window), chunks=int(st.counts.get("chunks", 0)))
                del window, results, rows

    with Stage("process_merge_chunks") as st:
        writer = pq.ParquetWriter(CHUNKS_PATH, CHUNK_SCHEMA, compression="zstd")
        for f in sorted(parts_dir.glob("part-*.parquet")):
            t = pq.read_table(f, schema=CHUNK_SCHEMA)
            writer.write_table(t)
            st.add(chunks=t.num_rows)
        writer.close()
        st.add(chunks_bytes=CHUNKS_PATH.stat().st_size)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--chunk-only", action="store_true", help="docs.parquet에서 청킹만 다시")
    a = ap.parse_args()
    chunk_stage(a.workers) if a.chunk_only else main(a.workers)
