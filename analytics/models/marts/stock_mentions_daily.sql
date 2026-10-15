-- 종목별 하루 뉴스 언급 수 (최근 90일)
select
    s.code || '|' || d.pub_day   as key,
    s.code                       as stock_code,
    n.name                       as stock_name,
    d.pub_day,
    count(*)                     as news_docs
from {{ ref('stg_doc') }} d
cross join lateral unnest(d.stock_codes) as s(code)
left join {{ ref('stg_stock_names') }} n on n.code = s.code
where d.source_type = 'news' and d.published_at >= now() - interval '90 days'
group by s.code, n.name, d.pub_day
