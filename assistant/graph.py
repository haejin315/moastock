"""AI 비서 흐름 (LangGraph). 흐름은 코드가 쥐고, 모델은 '질문 해석·도구 고르기'와 '근거로 문장 쓰기'를 한다.

  understand ─▶ route ─┬─▶ gather ─▶ answer ─▶ verify ─▶ END
   (도구 계획)          ├─▶ refuse  (매수·매도 권유 요청)        ─▶ END
                       └─▶ scope   (주식·경제와 무관한 질문)    ─▶ END

gather: 계획한 도구들을 함께 실행한다 - docs(뉴스·공시 검색), quote(시세), fundamentals(투자 지표),
price_history(기간 주가), screen(조건 검색), relations(회사 관계 그래프). 결과는 모두 [번호] 근거가 된다.

스트리밍: 각 노드는 get_stream_writer()로 진행 상황·답변 토큰·출처를 흘려보낸다
(graph.stream(..., stream_mode="custom")).
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph

import llm
import metrics
import observability as obs
from config import ANSWER_MAX_TOKENS, DEFAULT_DAYS, DISCLAIMER, ROOT

EVIDENCE_TOTAL = 8                    # 도구 결과 + 문서 근거를 합친 최대 개수
EVIDENCE_TOTAL_MOVE = 12              # 등락 원인 질문 (요인 비교·공시 목록·시장 뉴스·종목 뉴스)
from retrieval import KST, Evidence, resolve_stocks, search
import tools

STOCK_NAMES = {s["code"]: s["name"] for s in json.loads(
    (ROOT / "public" / "data" / "snapshot.json").read_text(encoding="utf-8"))["stocks"]}


class State(TypedDict, total=False):
    question: str
    stocks: list[str]
    days: int | None
    period: tuple | None
    intent: str
    query: str
    source: str
    advice: bool
    plan: list[dict]                 # [{"tool": "quote", "args": {...}}, ...]
    evidence: list[Evidence]
    notes: list[str]
    answer: str
    attribution_fixed: int           # 인용 번호와 맞지 않아 바로잡은 언론사 이름 수
    checks: dict


# ---- 1. 질문 해석 ---------------------------------------------------------------------

# '최근 소식'·'요즘'처럼 기간 없이 최신 자료를 원하는 질문: 2주로 좁히고 새 자료일수록 점수를 더 준다
RECENT = re.compile(r"최근\s*(?:소식|뉴스|기사|동향|이슈|근황|공시|발표|이야기|상황)|요즘|근황|최신\s*(?:소식|뉴스|기사|동향|공시)")
_PERIOD = [(r"오늘|금일", 2), (r"어제|전일", 3), (r"이번\s*주|최근\s*일주일|지난\s*주", 10),
           (r"이번\s*달|한\s*달|최근\s*1\s*개월|지난\s*달", 35), (r"최근\s*(\d+)\s*일", None),
           (r"최근\s*(\d+)\s*주", None), (r"최근\s*(\d+)\s*개월", None), (r"올해|연초", 280),
           (RECENT.pattern, 14)]
# 주가가 '왜' 움직였는지 묻는 질문: 최근 주가 흐름 + 최근 뉴스(원인 분석 기사)를 함께 본다
_MOVE = r"떨어|하락|빠지|빠졌|빠져|급락|폭락|내리|내렸|약세|올라|오르|올랐|상승|급등|폭등|강세|반등"
MOVE = re.compile(rf"(왜|이유|원인|무슨\s*일|어째서)[^.?!]{{0,15}}({_MOVE})|({_MOVE})[^.?!]{{0,15}}(왜|이유|원인|무슨\s*일|어째서)")
MOVE_DAYS = 14


def parse_month(q: str, now: datetime | None = None):
    """'8월', '지난 7월' → 그 달 1일 ~ 다음 달 1일 (올해, 아직 안 온 달이면 작년)
    '6월말', '6월 기준', '6월 30일 현재'는 자료의 기준 시점(예: 반기말 재무)이지 공시가 나온 달이 아니라서 제외한다."""
    m = re.search(r"(?<!\d)(1[0-2]|[1-9])\s*월(?!\s*(?:말|기준|현재|\d{1,2}\s*일\s*(?:기준|현재)))", q)
    if not m:
        return None
    now = now or datetime.now(KST)
    month = int(m.group(1))
    year = now.year if month <= now.month else now.year - 1
    start = datetime(year, month, 1, tzinfo=KST)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=KST)
    return start, end


def parse_days(q: str) -> int | None:
    for pat, days in _PERIOD:
        m = re.search(pat, q)
        if not m:
            continue
        if days is not None:
            return days
        n = int(m.group(1))
        return n * (1 if "일" in pat else 7 if "주" in pat else 31)
    return None


UNDERSTAND_SYS = """너는 주식 정보 서비스의 질문 분석기다. 사용자의 질문을 보고 JSON 하나만 출력한다.
필드:
- intent: "실적" | "공시" | "이슈" | "시황" | "비교" | "시세" | "지표" | "검색" | "관계" | "일반" | "범위밖" 중 하나
  (주식·경제·기업과 무관하면 "범위밖")
- advice: 특정 종목을 사라/팔라/오를까 같은 매매 판단·추천을 요구하면 true. 지표 조건으로 종목을 찾는 것은 false
- source: 공시 위주면 "dart", 뉴스·이슈 위주면 "news", 둘 다면 "any"
- query: 문서 검색에 쓸 짧은 한국어 문장 (회사명과 핵심 주제, 20자 안팎)
- tools: 필요한 도구 이름 목록 (여러 개 가능)
  "docs" 뉴스·공시 문서 검색 / "quote" 지금 주가 / "fundamentals" PER·PBR·시가총액·배당수익률 등 지표 /
  "price_history" 최근 N일 주가 흐름·수익률 / "screen" 조건으로 종목 찾기 / "relations" 최대주주·종속회사·계열회사·임원
- days: price_history 기간(일). 없으면 null
- screen: screen 을 고른 경우만 {"industry": 업종 단어 또는 null, "filters": [{"field": "per|pbr|dividendYield|marketCap|foreignRate|change", "op": "<=|>=", "value": 숫자}], "sort": 필드, "order": "asc|desc", "market": "KOSPI|KOSDAQ" 또는 null}
  (marketCap 은 원 단위 숫자, dividendYield·foreignRate·change 는 % 숫자)
- relation: relations 를 고른 경우 "최대주주" | "종속회사" | "계열회사" | "임원" | null
JSON 외의 말은 쓰지 않는다."""

# 모델이 도구를 빠뜨려도 질문에 명백한 신호가 있으면 붙인다 (작은 모델 보강)
TOOL_RULES = [
    ("quote", r"주가|시세|현재가|얼마에|지금\s*가격|종가"),
    ("fundamentals", r"PER|PBR|EPS|BPS|per|pbr|배당수익률|시가총액|시총|외국인\s*(보유|지분)|52주|밸류|주가수익비율|주가순자산"),
    ("price_history", r"수익률|올랐|내렸|떨어|상승률|하락률|추이|흐름|최고가|최저가|급락|폭락|급등|폭등"),
    ("docs", r"뉴스|공시|소식|이슈|기사|발표|보도"),
    ("relations", r"최대주주|대주주|자회사|종속회사|계열사|계열회사|임원|사내이사|사외이사|대표이사|지분\s*구조"),
]
SCREEN_RULE = (r"(종목|회사|기업).{0,12}(찾아|골라|목록|리스트|순위|어디|뭐|있어)"
               r"|(이하|이상|미만|초과|높은|낮은|상위|하위).{0,8}(종목|회사|기업)")
TOOLS = {"docs", "quote", "fundamentals", "price_history", "screen", "relations", "market"}
PER_STOCK = ("quote", "fundamentals", "price_history", "relations", "market")


def make_plan(q: str, j: dict, stocks: list[str], intent: str) -> list[dict]:
    chosen = [t for t in (j.get("tools") or []) if t in TOOLS]
    for name, pat in TOOL_RULES:
        if re.search(pat, q) and name not in chosen:
            chosen.append(name)
    if re.search(SCREEN_RULE, q) and not stocks and "screen" not in chosen:
        chosen.append("screen")
    if not stocks:                                       # 종목별 도구는 종목이 있어야 한다
        chosen = [t for t in chosen if t not in PER_STOCK]
    if stocks:
        chosen = [t for t in chosen if t != "screen"]
    move = bool(MOVE.search(q))
    if move:                     # 등락 원인: 시세 + 요인 비교(시장·업종·해외·공시 목록·시장 뉴스) + 종목 뉴스
        if stocks:               # 주가 흐름은 요인 비교에 들어 있다
            chosen = [t for t in chosen if t != "price_history"]
            chosen += [t for t in ("quote", "market") if t not in chosen]
        chosen += [] if "docs" in chosen else ["docs"]
    if not chosen or (intent in ("실적", "공시", "이슈", "시황", "비교") and "docs" not in chosen):
        chosen.append("docs")
    plan = []
    for t in dict.fromkeys(chosen):
        args = {}
        if t == "price_history":
            args["days"] = j.get("days") or parse_days(q) or (MOVE_DAYS if move else 30)
        elif t == "screen":
            sc = j.get("screen") if isinstance(j.get("screen"), dict) else {}
            market = sc.get("market") if sc.get("market") in ("KOSPI", "KOSDAQ") else None
            market = "KOSPI" if "코스피" in q else "KOSDAQ" if "코스닥" in q else market
            args = {"industry": sc.get("industry"), "filters": sc.get("filters") or [], "sort": sc.get("sort"),
                    "order": sc.get("order") or "desc", "market": market}
        elif t == "relations":
            args["relation"] = j.get("relation")
        plan.append({"tool": t, "args": args})
    return plan


@obs.observe(name="node.understand", capture_input=False, capture_output=False)
def understand(state: State) -> State:
    w = get_stream_writer()
    w({"type": "status", "text": "질문을 이해하는 중"})
    q = state["question"]
    stocks = resolve_stocks(q)[:3]
    days = parse_days(q)
    period = parse_month(q)
    j = llm.complete_json([{"role": "system", "content": UNDERSTAND_SYS}, {"role": "user", "content": q}],
                          max_tokens=400)
    intents = {"실적", "공시", "이슈", "시황", "비교", "시세", "지표", "검색", "관계", "일반", "범위밖"}
    intent = j.get("intent") if j.get("intent") in intents else "일반"
    source = j.get("source") if j.get("source") in {"news", "dart", "any"} else "any"
    query = str(j.get("query") or q)[:80]
    if MOVE.search(q):          # 등락 원인은 공시보다 시장 해석 기사가 답이다. 최근 2주부터 (못 찾으면 find_docs 가 넓힌다)
        intent = "이슈" if intent in ("일반", "시세", "공시", "검색") else intent
        source, days = "news", days or MOVE_DAYS
    plan = make_plan(q, j, stocks, intent)
    # 규칙 보강: 모델이 놓쳐도 명백한 매매 권유 요청은 거른다. 지표 조건으로 종목을 찾는 것은 권유가 아니다
    advice = bool(re.search(r"(사도|팔아도|매수해도|매도해도|살까|팔까|사야|팔아야|들어가도|물타기|손절)\s*(될까|돼|할까|하나|해|요)?", q))
    if j.get("advice") and not any(p["tool"] == "screen" and p["args"].get("filters") for p in plan):
        advice = True
    names = [STOCK_NAMES.get(c, c) for c in stocks]
    w({"type": "plan", "intent": intent, "stocks": names, "days": days or DEFAULT_DAYS, "query": query,
       "period": [d.strftime("%Y-%m-%d") for d in period] if period else None,
       "tools": [p["tool"] for p in plan]})
    obs.span(output={"intent": intent, "stocks": stocks, "days": days, "query": query, "advice": advice,
                     "tools": [p["tool"] for p in plan]})
    return {"stocks": stocks, "days": days, "period": period, "intent": intent, "query": query, "source": source,
            "advice": advice, "plan": plan, "notes": []}


def route(state: State) -> str:
    if state.get("advice"):
        return "refuse"
    if state.get("intent") == "범위밖":
        return "scope"
    return "gather"


# ---- 2. 거절·범위 밖 -----------------------------------------------------------------

def refuse(state: State) -> State:
    text = ("특정 종목을 사거나 팔지에 대한 판단이나 추천은 드릴 수 없습니다. "
            "대신 해당 종목의 최근 공시·뉴스·실적 같은 사실 정보를 정리해 드릴 수 있어요. "
            "예: \"삼성전자 최근 실적 공시 정리해줘\"")
    get_stream_writer()({"type": "token", "text": text})
    return {"answer": text, "evidence": [], "checks": {"refused": True}}


def scope(state: State) -> State:
    text = "모아스톡 AI 비서는 국내 상장사의 뉴스·공시·시세에 관한 질문에 답합니다. 종목이나 시장에 대해 물어봐 주세요."
    get_stream_writer()({"type": "token", "text": text})
    return {"answer": text, "evidence": [], "checks": {"out_of_scope": True}}


# ---- 3. 근거 모으기 (도구 실행) ----------------------------------------------------------

TOOL_STATUS = {"docs": "관련 뉴스·공시를 찾는 중", "quote": "시세를 확인하는 중", "fundamentals": "투자 지표를 확인하는 중",
               "price_history": "주가 흐름을 확인하는 중", "screen": "조건에 맞는 종목을 찾는 중",
               "relations": "회사 관계를 확인하는 중", "market": "시장·업종·해외 흐름과 비교하는 중"}


def find_docs(state: State) -> tuple[list[Evidence], list[str]]:
    stocks, days = state.get("stocks") or None, state.get("days") or DEFAULT_DAYS
    source = None if state.get("source") == "any" else state.get("source")
    notes = []
    q = state.get("query") or state["question"]
    period = state.get("period")
    recent = bool(RECENT.search(state["question"]) or MOVE.search(state["question"])) and not period
    ev = search(q, stocks=stocks, since_days=days, period=period, source=source, recent=recent)
    # 못 찾으면 조건을 하나씩 풀어 다시 (풀었다는 사실은 답변에 알린다). 연결 회사 근거만 있으면 못 찾은 것으로 본다
    def few(ev):
        return sum(1 for e in ev if not e.via) < 2

    if few(ev) and days < DEFAULT_DAYS and not period:      # '최근 공시'인데 2주 안에 없으면 기간부터 넓힌다
        ev = search(q, stocks=stocks, since_days=DEFAULT_DAYS, source=source, recent=recent)
        notes.append(f"최근 {days}일 자료가 적어 {DEFAULT_DAYS}일로 넓혀 찾았습니다"); days = DEFAULT_DAYS
    if few(ev) and source:
        ev = search(q, stocks=stocks, since_days=days, period=period, recent=recent)
        notes.append("자료 종류 조건을 넓혀 찾았습니다")
    if few(ev) and period:
        ev = search(q, stocks=stocks, since_days=days); notes.append("말씀하신 달에서 찾지 못해 최근 기간으로 넓혔습니다")
    if few(ev) and days:
        ev = search(q, stocks=stocks, since_days=None, recent=recent); notes.append("기간 조건을 넓혀 찾았습니다")
    return ev, notes


def run_tool(step: dict, state: State) -> tuple[list[Evidence], list[str]]:
    t, args, stocks = step["tool"], step["args"], state.get("stocks") or []
    if t == "docs":
        return find_docs(state)
    if t == "quote":
        return tools.quote(stocks), []
    if t == "fundamentals":
        return tools.fundamentals(stocks), []
    if t == "price_history":
        return tools.price_history(stocks, args.get("days") or 30), []
    if t == "screen":
        return tools.screen(**args), []
    if t == "relations":
        import kg
        return kg.relations(stocks, args.get("relation")), []
    if t == "market":
        return market_evidence(stocks[0], state["question"]), []
    return [], []


DOWN = re.compile(r"떨어|하락|빠지|빠졌|빠져|급락|폭락|내리|내렸|약세")


def market_evidence(code: str, question: str) -> list[Evidence]:
    """등락 원인 질문의 바탕 자료: 요인 비교(계산)·최근 14일 공시 목록 + 시장 전체·업종 뉴스 (종목 뉴스는 docs 가 맡는다)"""
    from retrieval import conn
    rows = conn().execute(
        "SELECT published_at, title FROM doc WHERE source_type = 'dart' AND %s = ANY(stock_codes) "
        "AND published_at >= now() - interval '14 days' ORDER BY published_at DESC LIMIT 20", (code,)).fetchall()
    disclosures = [(p.astimezone(KST).strftime("%m-%d"), t) for p, t in rows]
    word = "하락" if DOWN.search(question) else "상승"
    ev = tools.market_context(code, disclosures, asked=word)
    industry = (tools._stock(code) or {}).get("industry") or ""
    sector = "반도체" if "반도체" in industry else industry.split(",")[0].split("과")[0]
    for q in (f"코스피 증시 {word} 원인 외국인 수급", f"{sector} 업종 주가 {word} 이유"):
        ev += search(q, stocks=None, since_days=7, source="news", recent=True, limit=2)
    return ev


@obs.observe(name="node.gather", capture_input=False, capture_output=False)
def gather(state: State) -> State:
    """계획한 도구를 함께 실행하고 결과를 하나의 근거 목록([1]…)으로 합친다. 실패한 도구는 알리고 넘어간다."""
    import contextvars
    from concurrent.futures import ThreadPoolExecutor
    w = get_stream_writer()
    plan = state.get("plan") or [{"tool": "docs", "args": {}}]
    for step in plan:
        w({"type": "status", "text": TOOL_STATUS.get(step["tool"], "자료를 모으는 중")})
    notes = list(state.get("notes") or [])
    results = {}
    with ThreadPoolExecutor(max_workers=len(plan)) as pool:
        futs = {step["tool"]: pool.submit(contextvars.copy_context().run, run_tool, step, state) for step in plan}
        for name, f in futs.items():
            try:
                results[name] = f.result(timeout=60)
            except Exception as e:                       # 도구 하나가 실패해도 나머지로 답한다
                print(f"[tool] {name} 실패: {e!r}", flush=True)
                metrics.TOOL_FAIL.labels(name).inc()
                results[name] = ([], [f"{name} 자료를 가져오지 못해 그 자료 없이 답했습니다"])
    ev = []
    for step in plan:                                    # 데이터 도구 → 문서 순으로 번호를 매긴다
        if step["tool"] != "docs":
            ev += results[step["tool"]][0]
            notes += results[step["tool"]][1]
    if "docs" in results:
        # 등락 원인 질문은 요인 비교·시장 뉴스에 종목 뉴스까지 모아야 해서 근거를 더 받는다 (시장 뉴스와 겹치는 기사는 뺀다)
        total, least = (EVIDENCE_TOTAL_MOVE, 4) if "market" in results else (EVIDENCE_TOTAL, 2)
        have = {e.chunk_id for e in ev if e.chunk_id}
        docs = [e for e in results["docs"][0] if e.chunk_id not in have]
        ev += docs[:max(least, total - len(ev))]
        notes += results["docs"][1]
    for i, e in enumerate(ev, 1):
        e.n = i
    w({"type": "evidence", "items": [e.citation() for e in ev]})
    return {"evidence": ev, "notes": notes}


# ---- 4. 답변 ------------------------------------------------------------------------

ANSWER_SYS = """너는 모아스톡의 AI 비서다. 아래 [근거]만 사용해서 한국어로 답한다.
규칙:
1. 근거에 없는 사실·숫자는 절대 쓰지 않는다. 근거가 부족하면 "확인된 자료에서는 찾지 못했습니다"라고 말한다.
2. 문장마다 끝에 근거 번호를 [1], [2]처럼 붙인다.
   숫자(금액·비율·날짜)는 근거에 적힌 표기 그대로 옮긴다. "16,419,204,526원"을 "164억…원"처럼 단위로 바꿔 쓰지 않는다.
3. 날짜가 중요하면 시점과 자료 종류를 밝힌다. 공시면 "N월 N일 공시에 따르면", 뉴스면 "N월 N일 ○○(언론사) 보도에 따르면".
   뉴스를 공시라고 부르지 않는다. 언론사 이름은 그 문장에 붙이는 번호의 근거 머리에 적힌 언론사를 쓴다.
4. 매수·매도 추천이나 주가 예측은 하지 않는다.
5. 3~6문장 또는 짧은 목록으로 간결하게 답한다.
6. (공시 표) 근거의 '열:' 줄이 열 이름이고, 아래 각 줄은 '행 이름 — 값 · 값 …'으로 값이 열 순서대로 놓여 있다.
   값을 옮길 때는 행 이름과 열 이름(기간 등)을 함께 밝히고, 단위는 표의 '(단위 : …)'를 그대로 붙인다.
   표 제목에 '연결' 또는 '별도'가 있으면 반드시 "연결 기준"/"별도 기준"을 밝히고, 두 기준의 값을 섞지 않는다.
   예: "제58기 반기 현금배당금총액은 4,909,211백만원이다[2]." "연결 기준 2026년 6월말 현금및현금성자산은 92,916,382백만원이다[1]."
7. (모아스톡 데이터: 시세·지표·주가 흐름·조건 검색·회사 관계) 근거에 적힌 기준 시각·기준일을 함께 밝힌다.
   예: "10월 8일 11시 2분 기준 삼성전자 현재가는 268,500원이다[1]." 지표나 등락을 근거로 사라·팔라는 판단은 하지 않는다.
   조건 검색 결과는 '조건에 맞는 종목 목록'이라고 소개하고, 추천이라고 표현하지 않는다.
8. 근거에 '질문 종목의 종속회사/계열회사 … 자료'라고 적힌 것은 질문한 회사가 아니라 연결된 회사의 자료다.
   그 내용을 쓸 때는 "계열회사 삼성SDI는 …"처럼 어느 회사의 이야기인지 밝히고, 질문한 회사의 수치처럼 쓰지 않는다.
형식 예시: "8월 10일 공시에 따르면 회사는 50억원 규모의 전환사채 발행을 결정했다[1]. 전환가액은 1주당 3,200원이다[1]."
인용 번호 없이 끝나는 문장은 쓰지 않는다."""

# 주가가 왜 움직였는지 묻는 질문에만 덧붙인다: 자료를 요약하지 말고 요인을 가려 추론한다
MOVE_SYS = """
[등락 원인 질문 답변 방법]
사용자는 투자자이고 "왜 이렇게 움직였나"를 알고 싶어 한다. 공시·기사를 나열하지 말고, 모든 근거를 합쳐 요인을 추론한다.
0. 근거에 '※ 질문 확인' 줄이 있으면 질문의 전제가 틀린 것이다. 첫 문장에서 "최근 주가는 오히려 …"처럼 실제 움직임을 먼저 바로잡고,
   없는 상승·하락의 원인을 지어내지 말고 실제 움직임의 요인을 설명한다.
1. 움직임: '등락 요인 비교' 근거로 종목이 얼마나 움직였는지(1일·5거래일·20거래일)와 코스피·같은 업종과 비교해 밝힌다.
2. 요인의 몫: 같은 근거의 '나누기(계산)' 줄(1일·5거래일·20거래일)로 시장 전체·업종·종목 고유 중 어느 몫이 가장 큰지 말한다.
   예: "5거래일 -15.2% 중 시장 전체 몫이 -8.1%p로 가장 커, 회사 문제보다 시장 전체 하락의 영향이 크다[2]."
3. 가능성 높은 원인부터 2~4개를 '1) … 2) …' 목록으로 쓴다. 각 원인은 근거 기사·지표로 뒷받침한다.
   - 시장 전체 몫이 크면 시장 뉴스(외국인 매도·금리·환율·해외 증시)를, 업종 몫이 크면 업종·해외 비교 지표(해외 반도체지수 등)와
     업종 뉴스를, 종목 고유 몫이 크면 종목 뉴스와 공시를 앞에 둔다.
   - 기사의 해석은 "○○ 보도는 …를 원인으로 꼽았다"처럼 출처를 밝힌다.
   - 목록의 각 항목도 문장 끝마다 근거 번호를 붙인다. 예: "1) 외국인 매도: 10월 19일 연합뉴스TV 보도는 외국인 매도를 하락 원인으로 꼽았다[4]."
4. 공시: '최근 공시 목록'에 주가에 영향을 줄 만한 회사 사건(유상증자·전환사채·대규모 매도·실적·소송 등)이 있으면 원인으로 다루고,
   정기 보고·지분 변동 보고처럼 일상적인 공시뿐이면 "최근 2주 공시에는 주가를 크게 움직일 만한 회사 사건이 보이지 않는다"고 한 줄로 말한다.
5. 마지막에 "정리하면 …"으로 가장 유력한 설명을 한 문장으로 맺는다. 확정이 아니라 자료로 본 추론임을 드러낸다("…로 보인다").
6. 앞으로 오를지·내릴지 예측하거나 사라·팔라는 말은 하지 않는다. 6~10문장 안팎으로 쓴다."""


BIG = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3}){2,})(\s*원)?")


def with_korean_units(text: str) -> str:
    """큰 숫자 옆에 정확한 한글 단위 환산을 붙인다: '16,419,204,526원' → '16,419,204,526원(약 164.2억원)'.
    작은 모델이 원문 숫자를 억·만 단위로 직접 바꾸다 틀리는 대신, 맞게 환산된 값을 그대로 옮기게 한다."""
    def conv(m):
        v = int(m.group(1).replace(",", ""))
        unit = "원" if m.group(2) else ""
        if v >= 10**12:
            k = f"{v / 10**12:.2f}".rstrip("0").rstrip(".") + "조"
        elif v >= 10**8:
            k = f"{v / 10**8:.1f}".rstrip("0").rstrip(".") + "억"
        elif v >= 10**4:
            k = f"{v / 10**4:,.0f}만"
        else:
            return m.group(0)
        return f"{m.group(0)}(약 {k}{unit})"
    return BIG.sub(conv, text)


def evidence_block(ev: list[Evidence]) -> str:
    out = []
    for e in ev:
        date = e.published_at.strftime("%Y-%m-%d") if e.published_at else "날짜 미상"
        if e.kind == "data":             # 도구 결과 (시세·지표 등)
            when = f", {e.published_at:%Y-%m-%d %H:%M} 기준" if e.published_at else ""
            out.append(f"[{e.n}] (모아스톡 데이터{when}) {e.title}\n{e.text}")
            continue
        rel = f", {e.via} 자료" if e.via else ""    # 관계 그래프로 넓혀 찾은 연결 회사 문서
        if e.kind == "table":            # 표는 단위가 '(단위 : 백만원)' 등으로 따로 있어 원 단위 환산을 붙이지 않는다
            out.append(f"[{e.n}] (공시 표, {date}, {e.publisher or '-'}{rel}) {e.title}\n{e.text}")
            continue
        kind = "공시" if e.source_type == "dart" else "뉴스"
        out.append(f"[{e.n}] ({kind}, {date}, {e.publisher or '-'}{rel}) {e.title}\n{with_korean_units(e.text)}")
    return "\n\n".join(out)


@obs.observe(name="node.answer", capture_input=False, capture_output=False)
def answer(state: State) -> State:
    w = get_stream_writer()
    ev = state.get("evidence") or []
    if not ev:
        text = "질문과 관련된 자료를 찾지 못했습니다. 종목명이나 기간·조건을 바꿔 다시 물어봐 주세요."
        w({"type": "token", "text": text})
        return {"answer": text}
    w({"type": "status", "text": "근거를 바탕으로 답변을 쓰는 중"})
    move = any(e.doc_id == "tool:market" for e in ev)
    # 질문의 전제가 틀렸으면(오르지 않았는데 왜 오르냐) 첫 문장은 코드가 쓴다 - 작은 모델은 전제를 따라 원인을 지어낸다
    lead = premise_correction(ev)
    msgs = [{"role": "system", "content": ANSWER_SYS + (MOVE_SYS if move else "")},
            {"role": "user", "content": f"[근거]\n{evidence_block(ev)}\n\n[질문]\n{state['question']}" + (
                f"\n\n[이미 쓴 첫 문장]\n{lead}\n그 다음 문장부터 이어 쓴다. 질문이 말한 방향의 원인을 지어내지 말고, "
                "실제 움직임과 그 요인을 설명한다." if lead else "")}]
    # 문장이 끝날 때마다(인용 번호 뒤) 출처 이름을 바로잡아 내보낸다 - 사용자는 고친 문장만 본다
    parts, buf, fixed = [], "", 0
    if lead:
        parts.append(lead + " ")
        w({"type": "token", "text": lead + " "})
    def flush(upto: int):
        nonlocal buf, fixed
        out, n = fix_attribution(buf[:upto], ev)
        if lead and not parts[1:]:                  # 모델이 코드가 쓴 첫 문장을 되풀이하면 뺀다
            for sent in re.split(r"(?<=[.\]])\s+", lead):
                out = out.replace(sent, "", 1) if sent.strip() else out
            out = out.lstrip()
        buf, fixed = buf[upto:], fixed + n
        if out:
            parts.append(out)
            w({"type": "token", "text": out})
    for piece in llm.stream(msgs, max_tokens=ANSWER_MAX_TOKENS + (400 if move else 0)):
        buf += piece
        ends = [m.end() for m in SENT_END.finditer(buf)]
        if ends:
            flush(ends[-1])
    flush(len(buf))
    return {"answer": "".join(parts).strip(), "attribution_fixed": fixed}


PREMISE = re.compile(r"질문은 (상승|하락) 이유를 묻지만 실제로는 5거래일 ([+-][\d.]+%), 20거래일 ([+-][\d.]+%)")


def premise_correction(ev: list[Evidence]) -> str:
    """요인 비교 근거의 '※ 질문 확인' 줄 → 답변 첫 문장"""
    for e in ev:
        m = PREMISE.search(e.text) if e.doc_id == "tool:market" else None
        if m:
            name = e.title.replace(" 등락 요인 비교", "")
            return (f"질문과 달리 {name} 주가는 최근 뚜렷하게 {'오르지' if m.group(1) == '상승' else '내리지'} 않았습니다. "
                    f"5거래일 {m.group(2)}, 20거래일 {m.group(3)}입니다[{e.n}].")
    return ""


# 문장 끝: 인용 번호(와 마침표) 뒤의 공백, 또는 줄바꿈
SENT_END = re.compile(r"\][.!?]?(?=\s)|\n")


def fix_attribution(text: str, ev: list[Evidence]) -> tuple[str, int]:
    """'한국경제TV 보도에 따르면 …[6].'인데 [6]이 뉴스1 기사면 언론사 이름을 [6]의 언론사로 바꾼다.
    작은 모델은 근거 목록에서 옆 번호의 언론사를 가져다 쓰는 일이 잦다. 내용은 번호의 근거에서 왔다고 보고 이름을 고친다
    (번호가 여러 개면 그중 하나라도 맞으면 그대로, 모두 같은 다른 언론사를 가리킬 때만 고친다)."""
    pubs = sorted({e.publisher for e in ev if e.publisher and e.kind != "data" and e.source_type != "dart"},
                  key=len, reverse=True)
    if not pubs:
        return text, 0
    by_n = {e.n: e for e in ev}
    fixed = 0
    out = []
    for sent in re.split(r"((?<=[.!?])\s+|\n)", text):
        cited = [by_n[int(n)] for n in re.findall(r"\[(\d+)\]", sent) if int(n) in by_n]
        cited_pubs = {e.publisher for e in cited if e.kind != "data"}
        if len(cited_pubs) == 1 and None not in cited_pubs:
            right = next(iter(cited_pubs))
            taken = []                              # 긴 이름부터 ('한국경제TV'를 '한국경제'로 잘못 잡지 않게)
            for p in pubs:
                for m in re.finditer(re.escape(p), sent):
                    if any(a < m.end() and m.start() < b for a, b in taken):
                        continue
                    taken.append((m.start(), m.end()))
            wrong = [(a, b) for a, b in taken if sent[a:b] != right and sent[b:b + 6].lstrip().startswith(("보도", "는", "은", "에", "기사"))]
            for a, b in sorted(wrong, reverse=True):
                sent = sent[:a] + right + sent[b:]
                fixed += 1
        out.append(sent)
    return "".join(out), fixed


# ---- 5. 검증 ------------------------------------------------------------------------

# 숫자 표현: "12,400원", "3.5%", "202억 5천만 원", "300만 주"처럼 한글 단위가 섞인 것까지 하나로 잡는다
NUM_EXPR = re.compile(r"\d[\d,]*(?:\.\d+)?(?:\s*(?:조|억|만|천)(?:\s*\d[\d,]*(?:\.\d+)?)?)*")
UNIT = {"조": 10**12, "억": 10**8, "만": 10**4, "천": 10**3}


def korean_value(expr: str) -> float | None:
    """'202억 5천만' → 20250000000, '12,400' → 12400. 해석할 수 없으면 None."""
    s = re.sub(r"[,\s]", "", expr)
    if not s:
        return None
    total, cur = 0.0, ""
    big = 0.0                                  # 만 미만 자리(천)를 모았다가 만/억/조에서 곱한다
    try:
        for ch in s:
            if ch.isdigit() or ch == ".":
                cur += ch
            elif ch == "천":
                big += (float(cur) if cur else 1) * 1000; cur = ""
            elif ch in ("만", "억", "조"):
                part = big + (float(cur) if cur else 0)
                total += (part if part else 1) * UNIT[ch]; big, cur = 0.0, ""
        return total + big + (float(cur) if cur else 0)
    except ValueError:
        return None


def number_values(text: str) -> set[float]:
    return {v for m in NUM_EXPR.finditer(text) if (v := korean_value(m.group(0))) is not None}


def verify_answer(text: str, ev: list[Evidence]) -> dict:
    """인용 번호가 실제 근거를 가리키는지, 답변의 숫자가 근거 원문의 값과 일치하는지 검사.
    '164억 1천9만…'처럼 단위로 바꿔 쓴 숫자도 실제 값으로 계산해 비교하므로, 잘못 옮긴 숫자를 잡아낸다."""
    cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", text)})
    valid = [n for n in cited if 1 <= n <= len(ev)]
    src_vals = number_values(" ".join(e.text + " " + e.title for e in ev))
    body = re.sub(r"\[\d+\]", "", text)
    nums = []
    for m in NUM_EXPR.finditer(body):
        raw = m.group(0).strip()
        v = korean_value(raw)
        if v is None or (v < 10 and "." not in raw):        # 한 자리 정수(1일, 2회 등)는 검사하지 않는다
            continue
        nums.append((raw, v))
    # 일치: 0.1% 이내(반올림된 환산값 '약 164.2억' 포함) / 근사: 1% 이내(202.5억→'202억' 같은 절사) / 그 밖: 불일치
    def closest(v):
        return min((abs(v - s) / max(abs(s), 1e-9) for s in src_vals), default=1.0)
    exact = [r for r, v in nums if closest(v) <= 1e-3]
    approx = [r for r, v in nums if 1e-3 < closest(v) <= 1e-2]
    bad = [r for r, v in nums if closest(v) > 1e-2]
    return {
        "citations": len(cited), "invalid_citations": [n for n in cited if n not in valid],
        "numbers": len(nums), "numbers_supported": len(exact) + len(approx),
        "numbers_exact": len(exact), "numbers_approx": len(approx),
        "unsupported_numbers": bad[:5], "approx_numbers": approx[:5],
        "has_citation": bool(valid),
    }


@obs.observe(name="node.verify", capture_input=False, capture_output=False)
def verify(state: State) -> State:
    w = get_stream_writer()
    ev = state.get("evidence") or []
    checks = verify_answer(state.get("answer", ""), ev) if ev else {}
    if checks:                                   # 답변 품질을 트레이스 점수로 (Langfuse 에서 낮은 답변만 골라 보기)
        checks["attribution_fixed"] = state.get("attribution_fixed", 0)
        obs.score("attribution_fixed", float(checks["attribution_fixed"]))
        if checks.get("numbers"):
            obs.score("number_support", checks["numbers_supported"] / checks["numbers"])
        obs.score("has_citation", float(checks.get("has_citation", False)))
        obs.score("invalid_citations", float(len(checks.get("invalid_citations") or [])))
    obs.trace(output=state.get("answer", ""), metadata={"intent": state.get("intent"), "stocks": state.get("stocks"),
                                                       "evidence": [e.citation() for e in ev]})
    w({"type": "done", "checks": checks, "notes": state.get("notes") or [], "disclaimer": DISCLAIMER,
       "sources": [e.citation() for e in ev]})
    return {"checks": checks}


def build():
    g = StateGraph(State)
    for name, fn in [("understand", understand), ("gather", gather), ("answer", answer),
                     ("verify", verify), ("refuse", refuse), ("scope", scope)]:
        g.add_node(name, fn)
    g.set_entry_point("understand")
    g.add_conditional_edges("understand", route, {"gather": "gather", "refuse": "refuse", "scope": "scope"})
    g.add_edge("gather", "answer")
    g.add_edge("answer", "verify")
    g.add_edge("verify", END)
    for n in ("refuse", "scope"):
        g.add_edge(n, END)
    return g.compile()


GRAPH = build()
