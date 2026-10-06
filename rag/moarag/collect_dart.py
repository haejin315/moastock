"""3단계: DART 공시 목록 + 원문 수집 (금융감독원 OpenDART).

  python -m moarag.collect_dart list      # 기간 내 상장사(코스피 Y, 코스닥 K) 공시 목록
  python -m moarag.collect_dart docs [--limit N] [--rps 5]   # 원문(document.xml ZIP) → 텍스트

- corp_code 없이 목록을 조회하면 검색 기간이 3개월로 제한되므로 1개월 단위로 끊어 조회한다.
- OpenDART는 키당 하루 20,000건 제한이 있다. 상태코드 020(한도 초과)을 받으면 멈추고,
  다음 날 같은 명령을 다시 실행하면 status='pending'인 것부터 이어서 받는다.
- 원문 XML(DART 자체 마크업)은 표를 "셀 | 셀" 행으로 펴서 텍스트로 저장한다.
"""
from __future__ import annotations

import argparse
import asyncio
import html
import io
import re
import zipfile
from datetime import datetime, timedelta

from .config import DART_API_KEY, KST, NOW_KST, SINCE_KST
from .http import Fetcher
from .metrics import Progress, Stage
from .rawdb import connect

BASE = "https://opendart.fss.or.kr/api"

# 증권 발행 서류는 제외: 대부분 증권사의 ELS/DLS·채권 발행 정형문이라 건수·분량 대비 정보가 적다
# (투자설명서 한 건이 청크 1,800개에 이르기도 함). status='excluded'로 남겨 범위를 추적한다.
EXCLUDE = ["투자설명서", "일괄신고추가서류", "증권신고서", "증권발행실적보고서"]

# 원문 수집 우선순위 (일일 한도 안에서 가치 높은 것부터): 이벤트성 → 정기보고서 → 지분 보고
PRIORITY_SQL = """CASE
  WHEN report_nm LIKE '%임원ㆍ주요주주%' OR report_nm LIKE '%대량보유상황%' OR report_nm LIKE '%소유주식변동%' THEN 2
  WHEN report_nm LIKE '%사업보고서%' OR report_nm LIKE '%반기보고서%' OR report_nm LIKE '%분기보고서%' THEN 1
  ELSE 0 END"""


class QuotaExceeded(Exception):
    pass


def need_key():
    if not DART_API_KEY:
        raise SystemExit("DART_API_KEY가 없습니다. rag/.env 에 DART_API_KEY=... 를 추가하세요.")


# ---- 목록 ---------------------------------------------------------------------

def month_windows(start: datetime, end: datetime):
    cur = start
    while cur <= end:
        nxt = min(cur + timedelta(days=30), end)
        yield cur.strftime("%Y%m%d"), nxt.strftime("%Y%m%d")
        cur = nxt + timedelta(days=1)


async def collect_list():
    need_key()
    con = connect()
    with Stage("dart_list", since=SINCE_KST.date().isoformat()) as st:
        async with Fetcher(rps=5, concurrency=4) as f:
            for bgn, end in month_windows(SINCE_KST, NOW_KST):
                for cls in ("Y", "K"):
                    page, total_page = 1, 1
                    while page <= total_page:
                        _, _, body = await f.get(f"{BASE}/list.json", as_json=True, params={
                            "crtfc_key": DART_API_KEY, "bgn_de": bgn, "end_de": end,
                            "corp_cls": cls, "page_no": page, "page_count": 100})
                        code = body.get("status")
                        if code == "020":
                            raise QuotaExceeded(body.get("message"))
                        if code == "013":      # 조회 결과 없음
                            break
                        if code != "000":
                            raise RuntimeError(f"DART list 오류 {code}: {body.get('message')}")
                        total_page = int(body.get("total_page") or 1)
                        rows = body.get("list", [])
                        con.executemany(
                            "INSERT OR IGNORE INTO dart_filing(rcept_no,corp_code,corp_name,stock_code,corp_cls,"
                            "report_nm,flr_nm,rcept_dt,rm) VALUES(?,?,?,?,?,?,?,?,?)",
                            [(r["rcept_no"], r["corp_code"], r["corp_name"], r.get("stock_code"), r["corp_cls"],
                              r["report_nm"].strip(), r["flr_nm"], r["rcept_dt"], r.get("rm")) for r in rows])
                        con.commit()
                        st.add(pages=1, rows=len(rows))
                        page += 1
                    print(f"  {bgn}~{end} {cls}: 누적 {int(st.counts.get('rows', 0)):,}", flush=True)
            st.add(http_requests=f.stats["requests"])
        st.set(filings_total=con.execute("SELECT COUNT(*) FROM dart_filing").fetchone()[0])


# ---- 원문 ---------------------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")
_BLOCK_END = re.compile(r"</(P|TITLE|TR|TABLE|SECTION-\d|LIBRARY|COVER-TITLE|DIV|LI)>", re.I)
_CELL_END = re.compile(r"</(TD|TH|TE|TU)>", re.I)
_BR = re.compile(r"<BR\s*/?>", re.I)


def dart_xml_to_text(xml: str) -> str:
    """DART 마크업 → 평문. 표는 행 단위로 '셀 | 셀'."""
    xml = re.sub(r"<(STYLE|SCRIPT)[\s\S]*?</\1>", "", xml, flags=re.I)
    xml = _CELL_END.sub(" | ", xml)
    xml = _BLOCK_END.sub("\n", xml)
    xml = _BR.sub("\n", xml)
    text = html.unescape(_TAG.sub("", xml))
    lines = []
    for ln in text.splitlines():
        ln = re.sub(r"[ \t ]+", " ", ln).strip(" |")
        if ln:
            lines.append(ln)
    return "\n".join(lines)


def decode(b: bytes) -> str:
    for enc in ("utf-8", "euc-kr", "cp949"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace")


async def collect_docs(limit: int | None, rps: float):
    need_key()
    con = connect()
    for pat in EXCLUDE:
        con.execute("UPDATE dart_filing SET status='excluded' WHERE status='pending' AND report_nm LIKE ?",
                    (f"%{pat}%",))
    con.commit()
    rows = con.execute(
        f"SELECT rcept_no FROM dart_filing WHERE status='pending' "
        f"ORDER BY {PRIORITY_SQL}, rcept_dt DESC, rcept_no DESC"
        + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    with Stage("dart_docs", filings=len(rows), rps=rps) as st:
        prog = Progress("공시 원문", total=len(rows))
        stop = asyncio.Event()
        queue: asyncio.Queue = asyncio.Queue()
        for r in rows:
            queue.put_nowait(r[0])

        async with Fetcher(rps=rps, concurrency=6, timeout=120) as f:
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
                        if data[:2] != b"PK":            # ZIP이 아니면 오류 XML
                            msg = decode(data)
                            m = re.search(r"<status>(\d+)</status>", msg)
                            code = m.group(1) if m else str(status)
                            if code == "020":
                                stop.set()
                                raise QuotaExceeded(msg[:200])
                            con.execute("UPDATE dart_filing SET status='failed', error=?, fetched_at=? WHERE rcept_no=?",
                                        (f"{code}: {_TAG.sub('', msg)[:200]}", now, rno))
                            st.add(failed=1)
                        else:
                            zf = zipfile.ZipFile(io.BytesIO(data))
                            names = sorted(zf.namelist())
                            # 본문 파일(rcept_no.xml)을 먼저, 첨부는 뒤에
                            names.sort(key=lambda n: (not n.startswith(rno), n))
                            parts = [dart_xml_to_text(decode(zf.read(n))) for n in names]
                            body = "\n\n".join(p for p in parts if p)
                            con.execute(
                                "UPDATE dart_filing SET status=?, fetched_at=?, doc_files=?, doc_bytes=?, body=? "
                                "WHERE rcept_no=?",
                                ("ok" if body else "empty", now, len(names), len(data), body or None, rno))
                            st.add(ok=1, zip_bytes=len(data), body_chars=len(body))
                    except QuotaExceeded:
                        st.set(stopped="quota_exceeded")
                        print("  ! OpenDART 일일 한도 초과 - 내일 다시 실행하면 이어서 받습니다", flush=True)
                        return
                    except Exception as e:
                        con.execute("UPDATE dart_filing SET status='failed', error=?, fetched_at=? WHERE rcept_no=?",
                                    (str(e)[:300], now, rno))
                        st.add(failed=1)
                    con.commit()
                    prog.tick(ok=int(st.counts.get("ok", 0)))

            await asyncio.gather(*(worker() for _ in range(6)))
            st.add(http_requests=f.stats["requests"], bytes=f.stats["bytes"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["list", "docs"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--rps", type=float, default=5)
    a = ap.parse_args()
    asyncio.run(collect_list() if a.cmd == "list" else collect_docs(a.limit, a.rps))
