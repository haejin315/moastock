-- 종목 동시 언급: 쌍이 한 번씩만(stock_a < stock_b), 비어 있지 않게 (최근 30일 뉴스가 있는데 쌍이 0개면 계산이 깨진 것)
select 'duplicate_or_unordered' as problem, stock_a, stock_b
from {{ ref('news_comention') }}
group by stock_a, stock_b
having count(*) > 1 or stock_a >= stock_b
union all
select 'empty', null, null
where not exists (select 1 from {{ ref('news_comention') }})
