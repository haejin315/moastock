"""종목 동시 언급 (PySpark): 최근 N일 뉴스에서 두 종목이 한 기사에 함께 나온 횟수와 lift 를 계산한다.

  spark-submit --master local[2] comention.py --days 30

lift = P(A,B) / (P(A)·P(B)) = n_ab·N / (n_a·n_b). 1보다 크면 우연보다 자주 함께 나온다 - 같은 업종·공급망·
계열 관계 신호. 결과는 spark.news_comention 에 통째로 덮어쓴다 (dbt 모델 news_comention 이 회사명을 붙인다).
종목이 너무 많이 붙은 기사(시황 기사 등, 기본 8개 초과)는 관계 신호가 약해서 뺀다.
"""
import argparse
import os
from itertools import combinations

from pyspark.sql import SparkSession, functions as F, types as T

ap = argparse.ArgumentParser()
ap.add_argument("--days", type=int, default=30)
ap.add_argument("--max-stocks", type=int, default=8)
ap.add_argument("--min-pair", type=int, default=3)
a = ap.parse_args()

url, user, pw = os.environ["PG_URL"], os.environ["PG_USER"], os.environ["PG_PASSWORD"]
spark = SparkSession.builder.appName("moastock-comention").config("spark.sql.shuffle.partitions", "8").getOrCreate()
jdbc = {"url": url, "user": user, "password": pw, "driver": "org.postgresql.Driver"}

# 결과 스키마가 없으면 만든다 (JDBC 로 DDL 한 줄)
conn = spark._jvm.java.sql.DriverManager.getConnection(url, user, pw)
conn.createStatement().execute("CREATE SCHEMA IF NOT EXISTS spark")
conn.close()

docs = (spark.read.format("jdbc").options(**jdbc)
        .option("query", f"""SELECT doc_id, stock_codes FROM doc
                             WHERE source_type = 'news' AND published_at >= now() - interval '{int(a.days)} days'
                               AND cardinality(stock_codes) BETWEEN 1 AND {int(a.max_stocks)}""")
        .option("fetchsize", "5000").load())
docs = docs.withColumn("codes", F.array_sort(F.array_distinct("stock_codes"))).cache()
n_docs = docs.count()

per_stock = docs.select(F.explode("codes").alias("code")).groupBy("code").agg(F.count("*").alias("n"))

pairs_udf = F.udf(lambda cs: [list(p) for p in combinations(cs, 2)], T.ArrayType(T.ArrayType(T.StringType())))
pairs = (docs.where(F.size("codes") >= 2)
         .select(F.explode(pairs_udf("codes")).alias("p"))
         .select(F.col("p")[0].alias("stock_a"), F.col("p")[1].alias("stock_b"))
         .groupBy("stock_a", "stock_b").agg(F.count("*").alias("n_ab"))
         .where(F.col("n_ab") >= a.min_pair))

out = (pairs
       .join(per_stock.withColumnRenamed("code", "stock_a").withColumnRenamed("n", "n_a"), "stock_a")
       .join(per_stock.withColumnRenamed("code", "stock_b").withColumnRenamed("n", "n_b"), "stock_b")
       .withColumn("n_docs", F.lit(n_docs).cast("long"))
       .withColumn("lift", F.col("n_ab") * F.col("n_docs") / (F.col("n_a") * F.col("n_b")))
       .withColumn("window_days", F.lit(a.days).cast("int"))
       .withColumn("computed_at", F.current_timestamp())
       .select("stock_a", "stock_b", "n_ab", "n_a", "n_b", "n_docs", "lift", "window_days", "computed_at"))

# truncate: 표 정의(dbt on-run-start 가 만든 것)는 두고 내용만 바꾼다
(out.write.format("jdbc").options(**jdbc).option("dbtable", "spark.news_comention")
 .option("truncate", "true").mode("overwrite").save())
print(f"[comention] 뉴스 {n_docs:,}건, 종목쌍 {out.count():,}개 저장", flush=True)
spark.stop()
