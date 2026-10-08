"""3단계: DART 공시 목록 + 원문 수집 (금융감독원 OpenDART).

  python -m moarag.collect_dart list      # 기간 내 상장사(코스피 Y, 코스닥 K) 공시 목록
  python -m moarag.collect_dart docs [--limit N] [--rps 5]   # 원문(document.xml ZIP) → 텍스트

- corp_code 없이 목록을 조회하면 검색 기간이 3개월로 제한되므로 1개월 단위로 끊어 조회한다.
- OpenDART는 키당 하루 20,000건 제한이 있다. 상태코드 020(한도 초과)을 받으면 멈추고,
  다음 날 같은 명령을 다시 실행하면 status='pending'인 것부터 이어서 받는다.
- 원문 XML(DART 자체 마크업)의 표는 ROWSPAN·COLSPAN을 펼쳐 '| 열: 머리글…' 다음 행마다 '| 행이름 — 값 · 값' 한 줄로 저장한다.
  (원본 ZIP은 dart_zip 표에 보관 - 다시 받지 않고 재가공할 수 있게)
"""
from __future__ import annotations

import argparse
import asyncio
import html
import io
import re
import zipfile
from datetime import datetime, timedelta

from .config import DART_API_KEY, KST, SINCE_KST
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


async def collect_list(since: datetime | None = None):
    """since 를 주면 (매일 증분) 그날부터 오늘까지만."""
    need_key()
    con = connect()
    since = since or SINCE_KST
    with Stage("dart_list", since=since.date().isoformat()) as st:
        async with Fetcher(rps=5, concurrency=4) as f:
            for bgn, end in month_windows(since, datetime.now(KST)):
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


_TABLE = re.compile(r"<TABLE\b[^>]*>(.*?)</TABLE>", re.I | re.S)
_TR = re.compile(r"<TR\b[^>]*>(.*?)</TR>", re.I | re.S)
_CELL = re.compile(r"<(TD|TH|TE|TU)\b([^>]*)>(.*?)</\1>", re.I | re.S)
_SPAN = {k: re.compile(rf'\b{k}\s*=\s*"?(\d+)', re.I) for k in ("ROWSPAN", "COLSPAN")}
_NUMERIC = re.compile(r"^[\s\d,.()%△▲\-+~/]*$")
TABLE_ROW = "| "          # 표 행은 이 접두어로 시작한다 - 청킹에서 표를 문단과 따로 묶는 표시
TABLE_HEAD = "| 열: "     # 표 머리글 행 (청크마다 다시 붙인다)


def _cell_text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", _BR.sub(" ", s)))).strip()


def _table_grid(tbl: str):
    """TR/셀을 ROWSPAN·COLSPAN까지 펼친 격자. [(셀들, 머리글행 여부)]"""
    grid, pending = [], {}            # pending[col] = (남은 행 수, 값) - 위에서 내려오는 ROWSPAN
    for tr in _TR.findall(tbl):
        row, col = [], 0
        cells = _CELL.findall(tr)
        is_head = bool(cells) and all(t.upper() == "TH" for t, _, _ in cells)
        for tag, attrs, inner in cells:
            while col in pending:                 # 위 행에서 내려온 칸 채우기
                n, v = pending.pop(col)
                row.append(v)
                if n > 1:
                    pending[col] = (n - 1, v)
                col += 1
            rs = int((_SPAN["ROWSPAN"].search(attrs) or [0, 1])[1])
            cs = int((_SPAN["COLSPAN"].search(attrs) or [0, 1])[1])
            v = _cell_text(inner)
            for k in range(cs):
                row.append(v if (k == 0 or is_head) else "")   # 가로 병합: 머리글은 이름 반복, 본문은 첫 칸만
                if rs > 1:
                    pending[col] = (rs - 1, v)
                col += 1
        while col in pending:
            n, v = pending.pop(col)
            row.append(v)
            if n > 1:
                pending[col] = (n - 1, v)
            col += 1
        if any(row):
            grid.append((row, is_head))
    return grid


def table_to_lines(tbl: str) -> list[str]:
    """표 → 한 줄씩. 머리글이 있으면 첫 줄 '| 열: 행이름열 — 열1 · 열2 …', 이어서 '| 행이름 — 값1 · 값2 …'
    (빈 칸은 '-'로 자리를 지켜 열 순서가 맞게). 청킹에서 머리글 줄을 표 청크마다 다시 붙인다."""
    grid = _table_grid(tbl)
    head_rows = []
    while grid and grid[0][1]:
        head_rows.append(grid.pop(0)[0])
    # 머리글 둘째 줄이 TD로 적힌 경우(예: 기수 아래 날짜): 숫자 칸이 없고 첫 칸이 위 머리글에서 내려온 행
    while head_rows and len(grid) > 1:
        row = grid[0][0]
        if any(v and _NUMERIC.match(v) for v in row) or (row[0] and row[0] != head_rows[-1][0]):
            break
        head_rows.append(grid.pop(0)[0])
    width = max([len(r) for r, _ in grid] + [len(r) for r in head_rows] + [0])
    header = None
    if head_rows:
        header = []
        for c in range(width):
            parts = []
            for r in head_rows:
                v = r[c] if c < len(r) else ""
                if v and v not in parts:
                    parts.append(v)
            header.append(" ".join(parts))
    lines = []
    if header:
        # 앞쪽의 글자 칸(구분·항목명)은 행 이름 - 표 전체에서 글자만 있는 앞 열 수(최대 3)
        n_label = 0
        while n_label < min(3, width - 1) and all(
                not (c := (r[n_label] if n_label < len(r) else "")) or not _NUMERIC.match(c) for r, _ in grid):
            n_label += 1
        names = [h for h in header[:n_label] if h]
        lines.append(TABLE_HEAD + (" / ".join(dict.fromkeys(names)) + " — " if names else "")
                     + " · ".join(h or "-" for h in header[n_label:]))
        for row, _ in grid:
            row = row + [""] * (width - len(row))
            vals = row[n_label:]
            if all(v in ("", "-", "–", "0") for v in vals):     # 값이 하나도 없는 행(해당 없음)은 버린다
                continue
            label = " / ".join(dict.fromkeys(v for v in row[:n_label] if v))
            lines.append(TABLE_ROW + (label + " — " if label else "") + " · ".join(v or "-" for v in vals))
    else:
        for row, _ in grid:
            cells = list(row)
            while cells and not cells[-1]:
                cells.pop()
            if any(cells):
                lines.append(TABLE_ROW + " | ".join(cells))
    return lines


def dart_xml_to_text(xml: str) -> str:
    """DART 마크업 → 평문. 표는 '| '로 시작하는 줄들(ROWSPAN·COLSPAN 펼침, 첫 줄은 '| 열: ' 머리글)."""
    xml = re.sub(r"<(STYLE|SCRIPT)[\s\S]*?</\1>", "", xml, flags=re.I)
    xml = re.sub(r"\s+", " ", xml)          # 원본의 줄바꿈·들여쓰기는 의미가 없다 - 구조는 태그로만 판단
    tables: list[list[str]] = []

    def hold(m):                            # 표는 따로 펴 두고 자리표시만 남긴다 (아래 태그 제거에 다치지 않게)
        tables.append(table_to_lines(m.group(1)))
        return f"\n\x00T{len(tables) - 1}\x00\n"

    xml = _TABLE.sub(hold, xml)
    xml = _CELL_END.sub(" | ", xml)
    xml = _BLOCK_END.sub("\n", xml)
    xml = _BR.sub("\n", xml)
    text = html.unescape(_TAG.sub("", xml))
    lines = []
    for ln in text.splitlines():
        m = re.fullmatch(r"\s*\x00T(\d+)\x00\s*", ln)
        if m:
            lines.extend(tables[int(m.group(1))])
            continue
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
                                "UPDATE dart_filing SET status=?, fetched_at=?, doc_files=?, doc_bytes=?, body=?, "
                                "body_v=2 WHERE rcept_no=?",
                                ("ok" if body else "empty", now, len(names), len(data), body or None, rno))
                            con.execute("INSERT OR REPLACE INTO dart_zip (rcept_no, data, fetched_at) VALUES (?,?,?)",
                                        (rno, data, now))
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
