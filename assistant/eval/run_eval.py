"""평가 실행: questions.jsonl 의 질문을 흐름에 넣고 자동 채점한다.

지표
- 경로 정확도: 답변/거절/범위밖 중 기대한 경로로 갔는가
- 근거 적중(공시 질문): 정답 공시가 검색 근거에 들어갔는가, 답변이 그 근거를 인용했는가
- 인용: 인용이 있는 답변 비율, 없는 번호를 인용한 경우
- 숫자 근거율: 답변의 숫자 중 근거 원문에서 확인된 비율
- 지연시간: 질문당 전체 시간 p50/p95

  python eval/run_eval.py [--limit N] [--tag 이름]   (assistant/ 에서)
결과: eval/results/<tag>.json (질문별 상세 + 요약)
"""
import argparse
import json
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import LLM_BASE_URL, LLM_MODEL  # noqa: E402
from graph import GRAPH  # noqa: E402
from retrieval import conn  # noqa: E402


def _norm_report(title: str) -> str:
    """'[회사] [기재정정]주요사항보고서(전환사채권발행결정)' → '회사|전환사채권발행결정' (정정·괄호 표기 차이 무시)"""
    t = re.sub(r"\[(기재정정|첨부정정|첨부추가|변경등록|연장결정|발행조건확정)\]", "", title or "")
    t = re.sub(r"\s+", "", t)
    m = re.match(r"^\[([^\]]+)\](.*)$", t)
    corp, rest = (m.group(1), m.group(2)) if m else ("", t)
    inner = re.findall(r"\(([^()]*)\)", rest)
    kind = inner[0] if rest.startswith("주요사항보고서") and inner else re.sub(r"\(.*$", "", rest)
    return f"{corp}|{kind}"

HERE = Path(__file__).parent


def run_one(q):
    t0 = time.perf_counter()
    out = {"tokens": [], "evidence": [], "checks": {}, "plan": None}
    for ev in GRAPH.stream({"question": q["question"]}, stream_mode="custom"):
        k = ev.get("type")
        if k == "token":
            out["tokens"].append(ev["text"])
        elif k == "evidence":
            out["evidence"] = ev["items"]
        elif k == "plan":
            out["plan"] = ev
        elif k == "done":
            out["checks"] = ev["checks"]
    answer = "".join(out["tokens"])
    ms = (time.perf_counter() - t0) * 1000
    if "사거나 팔지에 대한 판단" in answer:
        route = "refuse"
    elif answer.startswith("모아스톡 AI 비서는 국내 상장사의"):
        route = "scope"
    else:
        route = "answer"
    ev_ids = [e["url"].split("rcpNo=")[-1] if "rcpNo=" in (e["url"] or "") else e["url"] for e in out["evidence"]]
    cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", answer)})
    res = {"id": q["id"], "kind": q["kind"], "question": q["question"], "route": route,
           "route_ok": route == q["expect"]["route"], "ms": round(ms), "answer": answer,
           "evidence": out["evidence"], "checks": out["checks"], "plan": out["plan"]}
    tools_want = q["expect"].get("tools")
    if tools_want:                      # 에이전트 도구 선택: 기대한 도구를 모두 골랐고, 그 도구 결과가 근거에 있는지
        chosen = (out["plan"] or {}).get("tools") or []
        res["tools_ok"] = all(t in chosen for t in tools_want)
        res["tool_evidence"] = any(e.get("type") == "data" or "관계 그래프" in (e.get("publisher") or "")
                                   for e in out["evidence"])
    want = q["expect"].get("doc_id")
    if want:
        rno = want.split(":", 1)[1]
        pos = [i + 1 for i, x in enumerate(ev_ids) if x == rno]
        res["hit"] = bool(pos)
        res["hit_cited"] = bool(pos) and any(p in cited for p in pos)
        # 같은 회사·같은 종류의 공시(정정본 포함)가 같은 달에 있으면 '동등 적중'
        row = conn().execute("SELECT title, published_at FROM doc WHERE doc_id = %s", (want,)).fetchone()
        if row:
            key, month = _norm_report(row[0]), row[1].strftime("%Y-%m")
            res["hit_equiv"] = any(_norm_report(e["title"]) == key and (e["published_at"] or "")[:7] == month
                                   for e in out["evidence"])
    return res


def summarize(rs):
    def rate(xs):
        xs = list(xs)
        return round(sum(xs) / len(xs), 3) if xs else None
    ans = [r for r in rs if r["route"] == "answer" and r["evidence"]]
    nums = sum(r["checks"].get("numbers", 0) for r in ans)
    sup = sum(r["checks"].get("numbers_supported", 0) for r in ans)
    lat = [r["ms"] for r in rs if r["route"] == "answer"]
    return {
        "questions": len(rs),
        "route_accuracy": rate(r["route_ok"] for r in rs),
        "dart_hit@evidence": rate(r["hit"] for r in rs if "hit" in r),
        "dart_hit_cited": rate(r["hit_cited"] for r in rs if "hit" in r),
        "dart_hit_equivalent": rate(r.get("hit_equiv", r["hit"]) for r in rs if "hit" in r),
        "tool_choice_accuracy": rate(r["tools_ok"] for r in rs if "tools_ok" in r),
        "tool_evidence_rate": rate(r["tool_evidence"] for r in rs if "tool_evidence" in r),
        "answers_with_citation": rate(r["checks"].get("has_citation", False) for r in ans),
        "answers_with_invalid_citation": sum(1 for r in ans if r["checks"].get("invalid_citations")),
        "number_support_rate": round(sup / nums, 3) if nums else None,
        "numbers_checked": nums,
        "latency_ms_p50": round(statistics.median(lat)) if lat else None,
        "latency_ms_p95": round(sorted(lat)[int(0.95 * (len(lat) - 1))]) if lat else None,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--tag", default=datetime.now().strftime("%Y%m%d-%H%M"))
    a = ap.parse_args()
    qs = [json.loads(l) for l in (HERE / "questions.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    if a.limit:
        qs = qs[: a.limit]
    rs = []
    for i, q in enumerate(qs, 1):
        r = run_one(q)
        rs.append(r)
        flag = "✓" if r["route_ok"] and r.get("hit", True) else "✗"
        print(f"[{i}/{len(qs)}] {flag} {r['id']} {r['route']} {r['ms']/1000:.1f}s hit={r.get('hit')} "
              f"cite={r['checks'].get('citations')} num={r['checks'].get('numbers_supported')}/{r['checks'].get('numbers')}",
              flush=True)
    s = summarize(rs)
    (HERE / "results").mkdir(exist_ok=True)
    out = HERE / "results" / f"{a.tag}.json"
    out.write_text(json.dumps({"model": LLM_MODEL, "base_url": LLM_BASE_URL, "summary": s, "results": rs},
                              ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps(s, ensure_ascii=False, indent=1))
    print("저장:", out)
