-- 관계 그래프가 갑자기 쪼그라들면 실패 (kg_build 가 표를 반쯤만 만들었다). 2026-10 기준 약 14만 간선
select count(*) as edges
from {{ source('moarag', 'kg_edge') }}
having count(*) < 100000
