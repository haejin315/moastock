-- 최근 3일 적재분에서 본문 해시가 같은 문서 (중복 제거가 망가졌다)
select source_type, content_sha1, count(*) as n
from {{ source('moarag', 'doc') }}
where crawled_at > now() - interval '3 days'
group by source_type, content_sha1
having count(*) > 1
