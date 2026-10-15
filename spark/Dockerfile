# PySpark 배치 작업 이미지 (Airflow 작업 파드에서 local 모드로 실행). Postgres 는 JDBC 로 읽고 쓴다.
FROM spark:3.5.3-python3
USER root
ADD https://repo1.maven.org/maven2/org/postgresql/postgresql/42.7.4/postgresql-42.7.4.jar /opt/spark/jars/
RUN chmod 644 /opt/spark/jars/postgresql-42.7.4.jar
COPY comention.py /app/comention.py
USER spark
