// GET /api/news?src=hk|yahoo|mw
// 언론사 RSS를 JSON으로 변환하는 프록시. 피드는 허용 목록으로만 제한한다
// (임의 URL 프록시가 되면 SSRF 구멍이 된다).
import { json, bad, cached, fetchUpstream, decodeEntities } from "./_utils.js";

const FEEDS = {
  hk: { name: "한국경제 증권", url: "https://www.hankyung.com/feed/finance", lang: "ko" },
  yahoo: { name: "Yahoo Finance", url: "https://finance.yahoo.com/news/rssindex", lang: "en" },
  mw: {
    name: "MarketWatch",
    url: "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    lang: "en",
  },
};

const CACHE_SEC = 300;
const MAX_ITEMS = 30;

function pick(block, tag) {
  const m = block.match(new RegExp(`<${tag}[^>]*>([\\s\\S]*?)</${tag}>`, "i"));
  if (!m) return "";
  return decodeEntities(
    m[1].replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1").replace(/<[^>]+>/g, ""),
  ).trim();
}

function parseRss(xml) {
  const items = [];
  for (const m of xml.matchAll(/<item[\s>][\s\S]*?<\/item>/gi)) {
    const block = m[0];
    const title = pick(block, "title");
    const link = pick(block, "link");
    if (!title || !link) continue;
    items.push({
      title,
      link,
      publishedAt: pick(block, "pubDate") || pick(block, "dc:date"),
    });
    if (items.length >= MAX_ITEMS) break;
  }
  return items;
}

export async function onRequestGet(context) {
  const src = new URL(context.request.url).searchParams.get("src") || "hk";
  const feed = FEEDS[src];
  if (!feed) return bad(`알 수 없는 피드: ${src} (허용: ${Object.keys(FEEDS).join(", ")})`);

  return cached(context, CACHE_SEC, async () => {
    try {
      const xml = await (await fetchUpstream(feed.url, "application/rss+xml, text/xml")).text();
      return json(
        { source: feed.name, lang: feed.lang, items: parseRss(xml) },
        { maxAge: CACHE_SEC },
      );
    } catch (err) {
      return json({ source: feed.name, lang: feed.lang, items: [], error: String(err.message) });
    }
  });
}
