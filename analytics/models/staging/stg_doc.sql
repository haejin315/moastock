-- 문서 메타 (본문은 길이·HTML 찌꺼기 여부만)
select
    doc_id,
    source_type,
    title,
    publisher,
    report_nm,
    stock_codes,
    content_sha1,
    published_at,
    crawled_at,
    (published_at at time zone 'Asia/Seoul')::date as pub_day,
    (crawled_at at time zone 'Asia/Seoul')::date   as load_day,
    length(body)                                    as body_len,
    body ~ '<(div|span|script|br)[ >/]'             as has_html
from {{ source('moarag', 'doc') }}
