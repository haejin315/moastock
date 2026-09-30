// GET /api/dart?page=1  또는  /api/dart?corp=삼성전자
// OpenDART 최신 공시 프록시. DART_API_KEY는 Cloudflare Pages 환경변수
// (Settings > Environment variables, Secret 권장)로 주입한다 - 키가 절대
// 브라우저로 내려가지 않는 것이 이 프록시의 존재 이유다.
import { json, bad, cached, fetchUpstream } from "./_utils.js";

const CACHE_SEC = 180;

function yyyymmdd(d) {
  return d.toISOString().slice(0, 10).replace(/-/g, "");
}

export async function onRequestGet(context) {
  const key = context.env.DART_API_KEY;
  if (!key) {
    return json({
      items: [],
      error: "key_missing",
      message:
        "DART_API_KEY가 설정되지 않았습니다. opendart.fss.or.kr에서 무료 발급 후 " +
        "Cloudflare Pages 환경변수로 추가하세요.",
    });
  }
  const params = new URL(context.request.url).searchParams;
  const page = Math.max(1, parseInt(params.get("page") || "1", 10) || 1);

  return cached(context, CACHE_SEC, async () => {
    const end = new Date();
    const begin = new Date(end.getTime() - 7 * 24 * 3600 * 1000);
    const qs = new URLSearchParams({
      crtfc_key: key,
      bgn_de: yyyymmdd(begin),
      end_de: yyyymmdd(end),
      page_no: String(page),
      page_count: "40",
    });
    const body = await (
      await fetchUpstream(`https://opendart.fss.or.kr/api/list.json?${qs}`)
    ).json();
    if (body.status !== "000" && body.status !== "013") {
      return bad(`DART 오류 ${body.status}: ${body.message}`, 502);
    }
    const items = (body.list || []).map((r) => ({
      corp: r.corp_name,
      market: { Y: "유가", K: "코스닥", N: "코넥스", E: "기타" }[r.corp_cls] || r.corp_cls,
      title: r.report_nm,
      date: r.rcept_dt,
      url: `https://dart.fss.or.kr/dsaf001/main.do?rcpNo=${r.rcept_no}`,
    }));
    return json(
      { items, page, totalPages: body.total_page || 1 },
      { maxAge: CACHE_SEC },
    );
  });
}
