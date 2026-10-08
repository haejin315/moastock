"""회사 관계 지식 그래프 만들기: 정기보고서의 표에서 관계(간선)를 뽑아 pgvector 와 같은 PostgreSQL 의 kg_edge 표에 둔다.

  python -m moarag.kg_build          (서버에서, 운영 DB 기준. 매일 증분 뒤에 다시 돌려도 된다 - 통째로 새로 만든다)

회사마다 가장 최근 정기보고서(사업·반기·분기) 하나만 쓴다. 표는 collect_dart 가 '| 열: …' 머리글과
'| 행이름 — 값 · 값' 행으로 펴 둔 형식을 다시 열 이름 → 값으로 맞춰 읽는다.

간선 (src → dst):
  최대주주등   회사 → 최대주주·특수관계인 (관계, 기말 지분율·주식수)
  최대주주     최대주주 법인 → 그 법인의 최대주주 (지분) - '최대주주의 최대주주'로 지배 구조를 따라갈 수 있다
  종속회사     회사 → 연결대상 종속회사 (주요사업, 자산총액, 주요종속회사 여부)
  계열회사     회사 → 같은 기업집단 계열회사 (상장 여부)
  임원         회사 → 임원 (직위, 등기 여부, 담당업무)
모든 간선에 출처(doc_id = 그 정기보고서)와 기준일(보고서 제출일)을 단다.
"""
from __future__ import annotations

import json
import re
import time

import psycopg

from .config import PG_DSN, SNAPSHOT
from .metrics import Stage

PERIODIC = r"사업보고서|반기보고서|분기보고서"
TABLE_HEAD, TABLE_ROW = "| 열: ", "| "
SCHEMA = """
CREATE TABLE IF NOT EXISTS kg_edge (
  src_code text,              -- 보고서 낸 회사 종목코드 (최대주주 법인 간선은 그 회사 기준)
  src_name text NOT NULL,
  rel      text NOT NULL,     -- 최대주주등 | 최대주주 | 종속회사 | 계열회사 | 임원
  dst_name text NOT NULL,
  dst_code text,              -- 상장사면 종목코드
  props    jsonb,
  rank     int,               -- 표 안 순서 (지분율·자산 순 정렬 전 원래 순서)
  doc_id   text NOT NULL,     -- 출처: 정기보고서
  as_of    timestamptz
);
"""


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def corp_key(name: str) -> str:
    """'삼성물산(주)', '㈜에코프로비엠(*1)' → '삼성물산', '에코프로비엠' (이름으로 상장사 찾기용)"""
    s = re.sub(r"\(\*[^)]*\)|\(주\d*\)|\(주\)|㈜|주식회사|\(유\)|\s", "", name or "")
    return s.lower()


def tables(body: str):
    """본문에서 (제목 줄, [열 이름], [{열: 값}]) 를 차례로 낸다."""
    lines = body.split("\n")
    i = 0
    while i < len(lines):
        if not lines[i].startswith(TABLE_HEAD):
            i += 1
            continue
        cap = next((lines[k] for k in range(i - 1, max(-1, i - 4), -1) if not lines[k].startswith(TABLE_ROW)), "")
        head = lines[i][len(TABLE_HEAD):]
        lab, _, val = head.partition(" — ") if " — " in head else ("", "", head)
        cols = ([c.strip() for c in lab.split(" / ")] if lab else []) + [c.strip() for c in val.split(" · ")]
        n_lab = len(lab.split(" / ")) if lab else 0
        rows = []
        j = i + 1
        while j < len(lines) and lines[j].startswith(TABLE_ROW) and not lines[j].startswith(TABLE_HEAD):
            r = lines[j][len(TABLE_ROW):]
            rl, _, rv = r.partition(" — ") if " — " in r else ("", "", r)
            labels = rl.split(" / ") if rl else []
            vals = rv.split(" · ")
            if n_lab and len(labels) < n_lab:          # 같은 값이 겹쳐 합쳐진 행이름은 앞 값으로 채운다
                labels = labels + [labels[-1] if labels else ""] * (n_lab - len(labels))
            cells = labels[:n_lab] + vals
            if len(cells) == len(cols):
                rows.append({norm(c): v.strip() for c, v in zip(cols, cells)})
            j += 1
        yield cap, [norm(c) for c in cols], rows
        i = j


def pick(row: dict, *keys: str) -> str | None:
    """열 이름에 keys 가 모두 들어간 첫 열의 값 ('-'·빈 값은 None)"""
    for c, v in row.items():
        if all(k in c for k in keys):
            return None if v in ("", "-", "–") else v
    return None


def pick_end(row: dict, suffix: str) -> str | None:
    """열 이름이 suffix 로 끝나는 열의 값 ('소유주식수및지분율기말지분율' 같은 묶음 머리글용)"""
    for c, v in row.items():
        if c.endswith(suffix):
            return None if v in ("", "-", "–") else v
    return None


def num(v: str | None) -> float | None:
    if not v:
        return None
    m = re.search(r"-?[\d,]+(?:\.\d+)?", v.replace("(", "-").replace(")", ""))
    try:
        return float(m.group(0).replace(",", "")) if m else None
    except ValueError:
        return None


def extract(code: str, name: str, body: str) -> list[dict]:
    edges = []

    def add(rel, dst, props, src_name=name, src_code=code):
        if dst and dst not in ("-", "계", "합계", "소계") and not dst.startswith("합"):
            edges.append({"src_code": src_code, "src_name": src_name, "rel": rel, "dst_name": dst.strip(),
                          "props": {k: v for k, v in props.items() if v not in (None, "")}})

    seen = set()
    for cap, cols, rows in tables(body):
        cs = set(cols)
        has = lambda *ks: any(all(k in c for k in ks) for c in cs)  # noqa: E731
        # 1) 최대주주 및 특수관계인의 주식소유 현황
        if has("성명") and has("관계") and any(c.endswith("기말지분율") for c in cs) and "sh" not in seen:
            seen.add("sh")
            for r in rows:
                add("최대주주등", pick(r, "성명"), {"관계": pick(r, "관계"), "주식종류": pick(r, "주식"),
                                                "기말지분율": num(pick_end(r, "기말지분율")),
                                                "기말주식수": num(pick_end(r, "기말주식수"))})
        # 2) 최대주주(법인)의 최대주주
        elif has("명칭") and has("최대주주", "성명"):
            for r in rows:
                org = pick(r, "명칭")
                add("최대주주", pick(r, "최대주주", "성명"), {"지분율": num(pick(r, "최대주주", "지분")),
                                                        "대표이사": pick(r, "대표이사", "성명")},
                    src_name=org or "", src_code=None)
        # 3) 연결대상 종속회사
        elif has("상호") and has("주요사업") and "sub" not in seen:
            seen.add("sub")
            for r in rows:
                add("종속회사", pick(r, "상호"), {"주요사업": pick(r, "주요사업"),
                                              "자산총액": num(pick(r, "자산총액")), "주요종속회사": pick(r, "주요종속"),
                                              "주소": pick(r, "주소")})
        # 4) 계열회사 현황(상세)
        elif has("상장여부") and has("기업명") and has("법인등록번호") and "aff" not in seen:
            seen.add("aff")
            for r in rows:
                add("계열회사", pick(r, "기업명"), {"상장여부": pick(r, "상장여부")})
        # 5) 임원 현황 (등기·미등기)
        elif has("성명") and has("직위") and has("담당업무") and not has("보수") and not has("겸직"):
            kind = "미등기" if "미등기" in cap or not has("등기임원여부") else None
            key = "exec_" + (kind or "등기")
            if key in seen:
                continue
            seen.add(key)
            for r in rows:
                add("임원", pick(r, "성명"), {"직위": pick(r, "직위"), "등기": kind or pick(r, "등기임원여부"),
                                            "상근": pick(r, "상근"), "담당업무": pick(r, "담당업무")})
    for i, e in enumerate(edges):
        e["rank"] = i
    return edges


def main():
    name_to_code = {}
    for s in json.loads(SNAPSHOT.read_text(encoding="utf-8"))["stocks"]:
        name_to_code.setdefault(corp_key(s["name"]), s["code"])
    con = psycopg.connect(PG_DSN, autocommit=True)
    with Stage("kg_build") as st:
        t0 = time.perf_counter()
        # 회사마다 가장 최근 정기보고서
        latest = con.execute(
            "SELECT DISTINCT ON (stock_codes[1]) doc_id, stock_codes[1], corp_name, published_at FROM doc "
            "WHERE source_type = 'dart' AND report_nm ~ %s AND cardinality(stock_codes) > 0 "
            "ORDER BY stock_codes[1], published_at DESC", (PERIODIC,)).fetchall()
        rows = []
        for doc_id, code, corp, as_of in latest:
            body = con.execute("SELECT body FROM doc WHERE doc_id = %s", (doc_id,)).fetchone()[0]
            for e in extract(code, corp, body):
                e.update(doc_id=doc_id, as_of=as_of,
                         dst_code=name_to_code.get(corp_key(e["dst_name"])))
                if e["src_code"] is None:
                    e["src_code"] = name_to_code.get(corp_key(e["src_name"]))
                rows.append(e)
            st.add(reports=1)
        with con.transaction():
            con.execute("DROP TABLE IF EXISTS kg_edge_new")
            con.execute(SCHEMA.replace("kg_edge", "kg_edge_new"))
            with con.cursor().copy("COPY kg_edge_new (src_code, src_name, rel, dst_name, dst_code, props, rank, doc_id, "
                                   "as_of) FROM STDIN") as cp:
                for e in rows:
                    cp.write_row([e["src_code"], e["src_name"], e["rel"], e["dst_name"], e["dst_code"],
                                  json.dumps(e["props"], ensure_ascii=False), e["rank"], e["doc_id"], e["as_of"]])
            con.execute("CREATE INDEX ON kg_edge_new (src_code, rel)")
            con.execute("CREATE INDEX ON kg_edge_new (dst_code, rel)")
            con.execute("CREATE INDEX ON kg_edge_new (src_name)")
            con.execute("DROP TABLE IF EXISTS kg_edge")
            con.execute("ALTER TABLE kg_edge_new RENAME TO kg_edge")
        by_rel = dict(con.execute("SELECT rel, count(*) FROM kg_edge GROUP BY rel").fetchall())
        st.set(edges=len(rows), seconds=round(time.perf_counter() - t0, 1), **{f"edges_{k}": v for k, v in by_rel.items()})
    print(f"간선 {len(rows):,}개 {by_rel}", flush=True)


if __name__ == "__main__":
    main()
