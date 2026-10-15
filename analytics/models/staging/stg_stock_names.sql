-- 종목코드 → 회사명 (관계 그래프에 나온 이름 중 가장 많이 쓰인 것)
with names as (
    select src_code as code, src_name as name from {{ ref('stg_kg_edge') }} where src_code is not null
    union all
    select dst_code, dst_name from {{ ref('stg_kg_edge') }} where dst_code is not null
)
select distinct on (code) code, name
from (select code, name, count(*) as n from names group by 1, 2) x
order by code, n desc
