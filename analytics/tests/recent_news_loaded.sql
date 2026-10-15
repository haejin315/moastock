-- 최근 3일 안에 적재된 뉴스가 없으면 실패 (수집이 멈췄다)
select 'no news loaded in 3 days' as problem
where not exists (
    select 1 from {{ source('moarag', 'doc') }}
    where source_type = 'news' and crawled_at > now() - interval '3 days'
)
