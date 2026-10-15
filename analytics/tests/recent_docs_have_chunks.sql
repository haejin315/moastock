-- 최근 3일 적재 문서 중 검색 조각이 하나도 없는 문서 (임베딩·적재가 중간에 끊겼다)
select d.doc_id
from {{ source('moarag', 'doc') }} d
where d.crawled_at > now() - interval '3 days'
  and not exists (select 1 from {{ source('moarag', 'chunk') }} c where c.doc_id = d.doc_id)
