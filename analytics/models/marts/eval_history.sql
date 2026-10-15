-- 평가 회차 기록 (Kubeflow 파이프라인) + 직전 회차 대비 변화
select
    *,
    number_support_rate - lag(number_support_rate) over (partition by source order by created_at) as number_support_delta,
    dart_hit_cited - lag(dart_hit_cited) over (partition by source order by created_at)            as dart_hit_cited_delta
from {{ source('eval', 'runs') }}
