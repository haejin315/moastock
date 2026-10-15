-- Spark 가 계산한 종목 동시 언급에 회사명을 붙인다 (lift > 1: 우연보다 자주 함께 나온다)
select
    c.stock_a, a.name as name_a,
    c.stock_b, b.name as name_b,
    c.n_ab, c.n_a, c.n_b, c.n_docs,
    round(c.lift::numeric, 2) as lift,
    c.window_days, c.computed_at
from {{ source('spark', 'news_comention') }} c
left join {{ ref('stg_stock_names') }} a on a.code = c.stock_a
left join {{ ref('stg_stock_names') }} b on b.code = c.stock_b
