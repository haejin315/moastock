{# 모델에 schema 를 주면 analytics_<이름> 이 아니라 그 이름 그대로 쓴다 #}
{% macro generate_schema_name(custom_schema_name, node) -%}
  {{ custom_schema_name | trim if custom_schema_name else target.schema }}
{%- endmacro %}
