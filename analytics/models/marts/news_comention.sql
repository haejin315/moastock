-- 종목 동시 언급: 최근 30일 뉴스에서 두 종목이 한 기사에 함께 나온 문서 수와 lift.
-- lift = n_ab·N / (n_a·n_b). 1보다 크면 우연보다 자주 함께 나온다 - 같은 업종·공급망·계열 관계 신호.
-- 종목이 너무 많이 붙은 기사(시황 기사 등, 8개 초과)는 관계 신호가 약해서 뺀다. 쌍은 3건 이상만.
-- (2026-10-19 까지는 Spark 작업이 계산했다. 수만 건 규모라 SQL 한 번이 더 빠르고 단순하다)
{% set days, max_stocks, min_pair = 30, 8, 3 %}
with docs as (
    select doc_id, array(select distinct c from unnest(stock_codes) c order by c) as codes
    from {{ ref('stg_doc') }}
    where source_type = 'news'
      and published_at >= now() - interval '{{ days }} days'
      and cardinality(stock_codes) between 1 and {{ max_stocks }}
),
total as (select count(*) as n_docs from docs),
per_stock as (
    select c as code, count(*) as n from docs, unnest(codes) c group by c
),
pairs as (
    select a.code as stock_a, b.code as stock_b, count(*) as n_ab
    from docs d
    cross join lateral unnest(d.codes) with ordinality a(code, i)
    cross join lateral unnest(d.codes) with ordinality b(code, j)
    where a.i < b.j
    group by 1, 2
    having count(*) >= {{ min_pair }}
)
select
    p.stock_a, na.name as name_a,
    p.stock_b, nb.name as name_b,
    p.n_ab, sa.n as n_a, sb.n as n_b, t.n_docs,
    round((p.n_ab * t.n_docs)::numeric / (sa.n * sb.n), 2) as lift,
    {{ days }} as window_days,
    now() as computed_at
from pairs p
join per_stock sa on sa.code = p.stock_a
join per_stock sb on sb.code = p.stock_b
cross join total t
left join {{ ref('stg_stock_names') }} na on na.code = p.stock_a
left join {{ ref('stg_stock_names') }} nb on nb.code = p.stock_b
