select src_code, src_name, rel, dst_code, dst_name, props, rank, doc_id, as_of
from {{ source('moarag', 'kg_edge') }}
