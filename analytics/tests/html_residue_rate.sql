-- HTML 태그가 남은 문서가 최근 적재분의 1%를 넘으면 경고 (정제 규칙이 새 기사 형식을 놓쳤다)
{{ config(severity='warn') }}
select load_day, sum(html_docs) as html, sum(docs) as docs
from {{ ref('ingest_daily') }}
where load_day > current_date - 3
group by load_day
having sum(html_docs) > 0.01 * sum(docs)
