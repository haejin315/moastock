# dbt 실행 이미지 (Airflow 작업 파드). 프로젝트 파일을 이미지에 넣는다.
FROM python:3.12-slim
RUN pip install --no-cache-dir "dbt-core==1.9.4" "dbt-postgres==1.9.0"
WORKDIR /app
COPY . /app
ENTRYPOINT []
