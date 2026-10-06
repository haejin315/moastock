# OpenSearch + 한국어 형태소 분석기(nori). 키워드(BM25)·하이브리드 검색에 필요하다.
FROM opensearchproject/opensearch:3.3.0
RUN /usr/share/opensearch/bin/opensearch-plugin install --batch analysis-nori
