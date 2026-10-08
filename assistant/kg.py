"""회사 관계 지식 그래프 조회 (relations 도구). 그래프는 rag/moarag/kg_build.py 가 정기보고서 표로 만든 kg_edge 표.

relations(종목코드들, 관계) → 관계별 근거(Evidence). 출처는 그 관계를 뽑은 정기보고서(DART 링크)다.
최대주주는 '최대주주의 최대주주'까지 따라가 지배 구조를 한 줄로 보여 준다.
"""
from __future__ import annotations

import json

from retrieval import KST, Evidence, conn

REL_ALIASES = {"최대주주": ["최대주주등"], "종속회사": ["종속회사"], "계열회사": ["계열회사"], "임원": ["임원"],
               None: ["최대주주등", "종속회사", "계열회사", "임원"]}
LIMIT = {"최대주주등": 10, "종속회사": 15, "계열회사": 30, "임원": 12}


def _edges(code: str, rel: str):
    sql = ("SELECT e.src_name, e.dst_name, e.props, e.doc_id, e.as_of, d.title, d.url FROM kg_edge e "
           "JOIN doc d ON d.doc_id = e.doc_id WHERE e.src_code = %s AND e.rel = %s ORDER BY e.rank")
    return conn().execute(sql, (code, rel)).fetchall()


def _chain(start: str, depth: int = 3) -> list[str]:
    """최대주주 법인 → 그 법인의 최대주주 … (보고서의 '최대주주의 최대주주' 표에서)"""
    out, cur = [], start
    for _ in range(depth):
        r = conn().execute("SELECT dst_name, props FROM kg_edge WHERE rel = '최대주주' AND src_name = %s LIMIT 1",
                           (cur,)).fetchone()
        if not r:
            break
        p = r[1] if isinstance(r[1], dict) else json.loads(r[1] or "{}")
        out.append(f"{cur}의 최대주주는 {r[0]}" + (f"(지분 {p['지분율']:g}%)" if p.get("지분율") is not None else ""))
        cur = r[0]
    return out


def _fmt(rel: str, rows) -> list[str]:
    lines = []
    for src, dst, props, *_ in rows:
        p = props if isinstance(props, dict) else json.loads(props or "{}")
        if rel == "최대주주등":
            lines.append(f"{dst} ({p.get('관계') or '-'}, {p.get('주식종류') or ''}) 기말 지분율 "
                         f"{p['기말지분율']:g}%" if p.get("기말지분율") is not None else f"{dst} ({p.get('관계') or '-'})")
        elif rel == "종속회사":
            extra = " · ".join(x for x in [p.get("주요사업"), f"자산총액 {p['자산총액']:,.0f}" if p.get("자산총액") else None,
                                           "주요종속회사" if (p.get("주요종속회사") or "").startswith(("O", "해당", "예")) else None] if x)
            lines.append(f"{dst} — {extra}" if extra else dst)
        elif rel == "계열회사":
            lines.append(f"{dst}({p.get('상장여부') or '-'})")
        elif rel == "임원":
            lines.append(" · ".join(x for x in [dst, p.get("직위"), p.get("등기"), p.get("담당업무")] if x))
    return lines


def relations(codes: list[str], relation: str | None = None) -> list[Evidence]:
    out = []
    for code in codes:
        for rel in REL_ALIASES.get(relation, REL_ALIASES[None]):
            rows = _edges(code, rel)
            if not rows:
                continue
            src, doc_id, as_of, title, url = rows[0][0], rows[0][3], rows[0][4], rows[0][5], rows[0][6]
            if rel == "임원":                         # 등기임원 먼저
                rows = sorted(rows, key=lambda r: (r[2] or {}).get("등기") == "미등기")
            if rel == "종속회사":                      # 주요종속회사·자산 큰 순
                rows = sorted(rows, key=lambda r: -((r[2] or {}).get("자산총액") or 0))
            if rel == "최대주주등":                    # 같은 주주의 보통주·우선주 줄은 보통주만
                rows = [r for r in rows if (r[2] or {}).get("주식종류") in (None, "보통주")] or rows
            total = len(rows)
            lines = _fmt(rel, rows[:LIMIT[rel]])
            label = {"최대주주등": "최대주주 및 특수관계인", "종속회사": "연결대상 종속회사", "계열회사": "계열회사",
                     "임원": "임원"}[rel]
            head = (f"{src}의 {label} — {title} 기준 ({as_of.astimezone(KST):%Y-%m-%d} 제출), "
                    f"전체 {total}건 중 {len(lines)}건")
            body = [head] + [f"- {x}" for x in lines]
            if rel == "최대주주등" and rows:
                chain = _chain(rows[0][1])
                if chain:
                    body.append("지배 구조: " + " → ".join(chain))
            out.append(Evidence(n=0, chunk_id="", doc_id=doc_id, text="\n".join(body), score=1.0,
                                title=f"{title} — {label}", publisher="DART 정기보고서(모아스톡 관계 그래프)",
                                published_at=as_of, url=url, original_url=None, source_type="dart",
                                report_nm=title, kind="data"))
    return out
