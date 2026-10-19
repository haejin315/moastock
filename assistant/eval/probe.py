"""사이트 경로 점검: 공개 API(/api/assistant)로 질문 몇 개를 보내 SSE 를 끝까지 읽는다.
Cloudflare Worker → 터널 → assistant → LLM 까지 사용자와 같은 경로를 본다. 질문은 사이트의 IP당 하루 한도에서 빠진다.

  python eval/probe.py [--url https://moastock.co.kr/api/assistant] [--min-ok 0.8]

성공 = 오류 없이 done 이벤트까지 오고 인용이 있음. 성공률이 min-ok 아래면 종료 코드 1.
"""
import argparse
import json
import statistics
import sys
import time

import httpx

QUESTIONS = ["삼성전자 최근 소식 알려줘", "SK하이닉스 최대주주는?", "현대차 최근 공시 알려줘",
             "카카오 PER 알려줘", "LG화학 배터리 자회사 최근 실적은?"]


def ask(client: httpx.Client, url: str, q: str):
    t0, first = time.time(), None
    with client.stream("POST", url, json={"question": q}, timeout=180) as r:
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} {r.read()[:200]!r}")
        kind = None
        for line in r.iter_lines():
            if line.startswith("event:"):
                kind = line[6:].strip()
            elif line.startswith("data:"):
                d = json.loads(line[5:])
                if kind == "token" and first is None:
                    first = time.time() - t0
                if kind == "error":
                    raise RuntimeError(d.get("message"))
                if kind == "done":
                    return bool(d.get("checks", {}).get("has_citation", True)), time.time() - t0, first
    raise RuntimeError("done 이벤트 없이 끊김")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://moastock.co.kr/api/assistant")
    ap.add_argument("--min-ok", type=float, default=0.8)
    a = ap.parse_args()

    with httpx.Client(headers={"User-Agent": "moastock-probe"}) as client:
        st = client.get(a.url, timeout=20).json()
        if not st.get("open"):
            sys.exit(f"운영 시간이 아님: {st}")
        oks, lat, firsts = [], [], []
        for q in QUESTIONS:
            t0 = time.time()
            try:
                ok, dt, first = ask(client, a.url, q)
                err = "" if ok else "인용 없음"
            except Exception as e:
                ok, dt, first, err = False, time.time() - t0, None, repr(e)
            oks.append(ok)
            lat.append(dt)
            if first is not None:
                firsts.append(first)
            print(f"{'OK  ' if ok else 'FAIL'} {dt:5.1f}s {q} {err}", flush=True)
    rate = sum(oks) / len(oks)
    print(f"성공률 {rate:.0%}, 응답 시간 중앙값 {statistics.median(lat):.1f}s"
          + (f", 첫 토큰 중앙값 {statistics.median(firsts):.1f}s" if firsts else ""), flush=True)
    sys.exit(0 if rate >= a.min_ok else 1)


if __name__ == "__main__":
    main()
