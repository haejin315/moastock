-- 회사별 관계 그래프 요약: 관계별 간선 수, 상장 이웃 수, 최대주주
with e as (select * from {{ ref('stg_kg_edge') }} where src_code is not null),
top_holder as (
    select distinct on (src_code) src_code, dst_name as largest_holder
    from e where rel = '최대주주등' order by src_code, rank
)
select
    e.src_code                                                       as stock_code,
    max(e.src_name)                                                  as stock_name,
    count(*) filter (where rel = '종속회사')                          as subsidiaries,
    count(*) filter (where rel = '계열회사')                          as affiliates,
    count(*) filter (where rel = '임원')                              as officers,
    count(*) filter (where rel = '최대주주등')                        as major_holders,
    count(distinct e.dst_code) filter (where e.dst_code is not null
                                      and e.dst_code <> e.src_code)  as listed_neighbors,
    max(t.largest_holder)                                            as largest_holder,
    max(e.as_of)                                                     as as_of
from e left join top_holder t using (src_code)
group by e.src_code
