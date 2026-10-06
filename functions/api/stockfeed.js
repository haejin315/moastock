// GET /api/stockfeed?code=005930&kind=news|disclosure
// 종목별 뉴스(네이버 뉴스 집계)와 공시(KOSCOM 공시, 네이버 제공)를 중계한다.
import { json, bad, cached, fetchUpstream, decodeEntities } from "./_utils.js";

const CACHE_SEC = 300;

export async function onRequestGet(context) {
  const params = new URL(context.request.url).searchParams;
  const code = (params.get("code") || "").trim();
  const kind = params.get("kind") || "news";
  if (!/^\d{6}$/.test(code)) return bad("code는 6자리 종목코드여야 합니다");
  if (kind !== "news" && kind !== "disclosure") return bad("kind는 news|disclosure");

  return cached(context, CACHE_SEC, async () => {
    if (kind === "disclosure") {
      const body = await (
        await fetchUpstream(`https://m.stock.naver.com/api/stock/${code}/disclosure?pageSize=20`)
      ).json();
      const items = (Array.isArray(body) ? body : []).map((d) => ({
        title: decodeEntities(d.title || ""),
        datetime: d.datetime,
        author: d.author || "",
        url: `https://m.stock.naver.com/domestic/stock/${code}/disclosure`,
      }));
      return json({ code, kind, items }, { maxAge: CACHE_SEC });
    }
    const body = await (
      await fetchUpstream(`https://m.stock.naver.com/api/news/stock/${code}?pageSize=20`)
    ).json();
    const items = [];
    for (const group of Array.isArray(body) ? body : []) {
      for (const n of group.items || []) {
        items.push({
          title: decodeEntities(String(n.title || "").replace(/<[^>]+>/g, "")),
          press: n.officeName || "",
          datetime: n.datetime || "",
          url: `https://n.news.naver.com/mnews/article/${n.officeId}/${n.articleId}`,
        });
        if (items.length >= 20) break;
      }
      if (items.length >= 20) break;
    }
    return json({ code, kind, items }, { maxAge: CACHE_SEC });
  });
}
