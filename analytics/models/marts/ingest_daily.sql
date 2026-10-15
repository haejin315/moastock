-- 적재일·자료별 수집량과 정제 품질 (최근 60일). 수집이 멈췄거나 정제가 망가지면 여기서 먼저 보인다
select
    load_day,
    source_type,
    count(*)                                                as docs,
    count(*) filter (where body_len < 200)                  as short_docs,
    count(*) filter (where has_html)                        as html_docs,
    count(*) filter (where cardinality(stock_codes) = 0)    as no_stock_docs,
    round(avg(body_len))::int                               as avg_body_len,
    max(published_at)                                       as latest_published_at
from {{ ref('stg_doc') }}
where crawled_at >= now() - interval '60 days'
group by 1, 2
