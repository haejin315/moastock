"""공시 원문 다시 받기 → 표를 살린 텍스트로 재가공 → 바뀐 문서만 청크 다시 만들기 (로컬).

처음 수집 때는 원문 XML의 줄바꿈을 그대로 둬서 표의 칸이 한 줄에 하나씩 흩어졌다. 원본 ZIP을 다시 받아
표를 행 단위로 펴고(collect_dart.dart_xml_to_text), 표 청크를 문단 청크와 따로 만든다(process.chunk_dart).

  python -m moarag.redo_dart fetch [--kind periodic|all] [--max 15000] [--rps 5]
      원문 ZIP을 다시 받아 dart_zip 표에 보관하고 dart_filing.body 를 새 방식으로 바꾼다 (body_v=2).
      하루 한도(키당 20,000건, 사이트와 함께 씀)를 넘지 않게 --max 로 끊고, 다음 날 같은 명령으로 이어간다.
  python -m moarag.redo_dart build
      body_v=2 인데 아직 반영 안 된 문서로 delta/ 아래 docs·chunks parquet 를 만든다 (서버에서 apply_delta 로 반영).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import re
import uuid
import zipfile
from datetime import datetime
from multiprocessing import Pool

import pyarrow as pa
import pyarrow.parquet as pq

from .collect_dart import BASE, QuotaExceeded, _TAG, dart_xml_to_text, decode, need_key
from .config import DART_API_KEY, DATA_DIR, DOCS_PATH, KST
from .http import Fetcher
from .metrics import Progress, Stage
from .process import CHUNK_SCHEMA, NS, PIPELINE_VERSION, chunk_worker, clean_dart, norm_for_hash
from .rawdb import connect

PERIODIC_SQL = "(report_nm LIKE '%사업보고서%' OR report_nm LIKE '%반기보고서%' OR report_nm LIKE '%분기보고서%')"
DELTA_DIR = DATA_DIR / "delta"


def _prepare(con):
    """(rawdb.connect 가 dart_zip 표와 body_v·applied_v 열을 만든다)"""


def zip_to_body(rno: str, data: bytes) -> str:
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = sorted(zf.namelist())
    names.sort(key=lambda n: (not n.startswith(rno), n))      # 본문 파일(rcept_no.xml) 먼저, 첨부는 뒤에
    return "\n\n".join(p for p in (dart_xml_to_text(decode(zf.read(n))) for n in names) if p)


async def fetch(kind: str, max_calls: int, rps: float):
    need_key()
    con = connect()
    _prepare(con)
    where = "status='ok' AND COALESCE(body_v,1) < 2" + (f" AND {PERIODIC_SQL}" if kind == "periodic" else "")
    rows = [r[0] for r in con.execute(
        f"SELECT rcept_no FROM dart_filing WHERE {where} ORDER BY rcept_dt DESC, rcept_no DESC LIMIT ?", (max_calls,))]
    with Stage("dart_refetch", filings=len(rows), kind=kind, rps=rps) as st:
        prog = Progress("공시 원문 다시 받기", total=len(rows))
        queue: asyncio.Queue = asyncio.Queue()
        for r in rows:
            queue.put_nowait(r)
        stop = asyncio.Event()
        async with Fetcher(rps=rps, concurrency=6, timeout=180) as f:
            async def worker():
                while not stop.is_set():
                    try:
                        rno = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    now = datetime.now(KST).isoformat(timespec="seconds")
                    try:
                        status, _, data = await f.get(f"{BASE}/document.xml", raw=True,
                                                      params={"crtfc_key": DART_API_KEY, "rcept_no": rno})
                        if data[:2] != b"PK":
                            msg = decode(data)
                            m = re.search(r"<status>(\d+)</status>", msg)
                            if (m.group(1) if m else "") == "020":
                                stop.set()
                                raise QuotaExceeded(msg[:200])
                            st.add(failed=1)
                            print(f"  실패 {rno}: {_TAG.sub('', msg)[:120]}", flush=True)
                        else:
                            body = zip_to_body(rno, data)
                            con.execute("INSERT OR REPLACE INTO dart_zip (rcept_no, data, fetched_at) VALUES (?,?,?)",
                                        (rno, data, now))
                            con.execute("UPDATE dart_filing SET body=?, body_v=2 WHERE rcept_no=?", (body, rno))
                            con.commit()
                            st.add(ok=1, zip_bytes=len(data), body_chars=len(body))
                    except QuotaExceeded:
                        print("  하루 한도 도달 - 내일 같은 명령으로 이어서", flush=True)
                        return
                    except Exception as e:                       # 한 건 실패로 전체를 멈추지 않는다
                        st.add(failed=1)
                        print(f"  오류 {rno}: {e!r}"[:200], flush=True)
                    prog.tick(1)
            await asyncio.gather(*(worker() for _ in range(6)))
    left = con.execute(f"SELECT count(*) FROM dart_filing WHERE {where}").fetchone()[0]
    print(f"남은 공시 {left:,}건", flush=True)


def build(workers: int):
    """body_v=2 인데 서버에 아직 반영하지 않은(applied_v<2) 공시의 새 문서·청크를 delta/ 에 쓴다."""
    con = connect()
    _prepare(con)
    todo = {r[0]: r[1] for r in con.execute(
        "SELECT rcept_no, body FROM dart_filing WHERE body_v=2 AND COALESCE(applied_v,1) < 2")}
    print(f"반영할 공시 {len(todo):,}건", flush=True)
    if not todo:
        return
    # 메타데이터(제목·종목·출처 등)는 기존 docs.parquet 의 것을 그대로 쓴다
    meta = {}
    for rb in pq.ParquetFile(DOCS_PATH).iter_batches(batch_size=20_000):
        for d in rb.to_pylist():
            if d["source_type"] == "dart" and d["rcept_no"] in todo:
                meta[d["rcept_no"]] = d
    names = dict(con.execute("SELECT code, name FROM stock"))
    DELTA_DIR.mkdir(exist_ok=True)
    docs_out, chunks_out = [], []
    with Stage("redo_dart_build", docs=len(meta), workers=workers) as st, Pool(workers) as pool:
        args = []
        for rno, d in meta.items():
            text = clean_dart(todo[rno])
            docs_out.append({"doc_id": d["doc_id"], "rcept_no": rno, "text": text,
                             "content_sha1": hashlib.sha1(norm_for_hash(text).encode()).hexdigest()})
            args.append((d["doc_id"], text, "dart"))
        prog = Progress("청킹", total=len(args))
        for (doc_id, spans), d in zip(pool.imap(chunk_worker, args, chunksize=4), meta.values()):
            seen, kept = set(), []
            for a, b, n, kind, t in spans:                       # 같은 문서 안의 똑같은 청크는 한 번만
                h = hashlib.sha1(norm_for_hash(t).encode()).digest()
                if h in seen:
                    continue
                seen.add(h)
                kept.append((a, b, n, kind, t, h.hex()))
            names_ = [names.get(c, c) for c in (d["stock_codes"] or [])]
            n_dup = len(json.loads(d["dup_sources"] or "[]"))
            for i, (a, b, n, kind, t, h) in enumerate(kept):
                chunks_out.append(dict(
                    chunk_id=str(uuid.uuid5(NS, f"{doc_id}#{i}")), doc_id=doc_id, chunk_index=i, n_chunks=len(kept),
                    text=t, char_start=a, char_end=b, n_tokens=n, chunk_sha1=h, source_type="dart",
                    title=d["title"], publisher=d["publisher"], author=d["author"], published_at=d["published_at"],
                    url=d["url"], original_url=d["original_url"], stock_codes=d["stock_codes"], stock_names=names_,
                    stock_match=d["stock_match"], corp_name=d["corp_name"], report_nm=d["report_nm"],
                    rcept_no=d["rcept_no"], section=d["section"], n_dup_sources=n_dup, crawled_at=d["crawled_at"],
                    kind=kind, pipeline_version=PIPELINE_VERSION))
                st.add(chunks=1, tokens=n, **{f"chunks_{kind}": 1})
            prog.tick(1)
    pq.write_table(pa.Table.from_pylist(docs_out), DELTA_DIR / "docs.parquet", compression="zstd")
    pq.write_table(pa.Table.from_pylist(chunks_out, schema=CHUNK_SCHEMA), DELTA_DIR / "chunks.parquet",
                   compression="zstd")
    (DELTA_DIR / "rcept_nos.json").write_text(json.dumps(sorted(meta)))
    print(f"문서 {len(docs_out):,} / 청크 {len(chunks_out):,} → {DELTA_DIR}", flush=True)


def mark_applied():
    """서버 반영(apply_delta)이 끝난 뒤: delta 에 들어간 공시를 applied_v=2 로 표시."""
    con = connect()
    _prepare(con)
    rnos = json.loads((DELTA_DIR / "rcept_nos.json").read_text())
    con.executemany("UPDATE dart_filing SET applied_v=2 WHERE rcept_no=?", [(r,) for r in rnos])
    con.commit()
    print(f"반영 표시 {len(rnos):,}건")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--kind", choices=["periodic", "all"], default="periodic")
    f.add_argument("--max", type=int, default=15000)
    f.add_argument("--rps", type=float, default=5)
    b = sub.add_parser("build")
    b.add_argument("--workers", type=int, default=8)
    sub.add_parser("mark-applied")
    a = ap.parse_args()
    if a.cmd == "fetch":
        asyncio.run(fetch(a.kind, a.max, a.rps))
    elif a.cmd == "build":
        build(a.workers)
    else:
        mark_applied()
