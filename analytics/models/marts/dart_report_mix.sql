-- 주별 공시 종류 (최근 12주). [기재정정] 같은 표시와 괄호 뒤는 떼고 묶는다
select
    date_trunc('week', pub_day)::date                                                 as week,
    regexp_replace(regexp_replace(report_nm, '\[[^]]*\]', '', 'g'), '\s*\(.*$', '')   as report_type,
    count(*)                                                                          as filings
from {{ ref('stg_doc') }}
where source_type = 'dart' and published_at >= now() - interval '84 days'
group by 1, 2
