"""测试 PostgreSQL + pgvector 的连接与向量检索。

关键点：postgres 参数不能直接传 Python list —— pgvector 的 psycopg 适配器只为
pgvector.Vector 和 numpy.ndarray 注册了 dumper，list 会被当成 smallint[] 数组发送，
报错 "操作符不存在: vector <-> smallint[]"。传参务必用 Vector(...) 包装。

运行： .venv\\Scripts\\python.exe test\\test_connect_pg.py
"""
import os
import sys

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector

HOST = os.getenv("PGHOST", "127.0.0.1")
PORT = os.getenv("PGPORT", "5432")
DBNAME = os.getenv("PGDATABASE", "pgvector_test")
USER = os.getenv("PGUSER", "postgres")
PASSWORD = os.getenv("PGPASSWORD", "postgres")

DSN = f"host={HOST} port={PORT} dbname={DBNAME} user={USER} password={PASSWORD}"


def main() -> int:
    try:
        conn = psycopg.connect(DSN)
    except psycopg.OperationalError as exc:
        print(f"[connect failed] {HOST}:{PORT}/{DBNAME} -> {exc}")
        print("检查项：服务是否运行(Get-Service postgresql-x64-17)、端口 5432、库名/密码")
        return 1

    with conn:
        # 1) 先确保扩展存在，再注册类型适配器（库里没有 vector 类型时 register_vector 会抛 ProgrammingError）
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        register_vector(conn)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            )
            version = cur.fetchone()[0]

            # 2) 建表 + 无数据时补种子数据，保证脚本可独立跑
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id        bigserial PRIMARY KEY,
                    embedding vector(3)
                )
                """
            )
            cur.execute("SELECT count(*) FROM items")
            if cur.fetchone()[0] == 0:
                cur.execute(
                    "INSERT INTO items (embedding) VALUES (%s), (%s), (%s), (%s)",
                    (Vector([1, 2, 3]), Vector([4, 5, 6]), Vector([7, 8, 9]), Vector([1, 2, 4])),
                )

            # 3) 相似度查询：<-> 是 L2 距离，<=> 是余弦距离
            query = Vector([3, 1, 2])
            cur.execute(
                """
                SELECT id,
                       embedding::text            AS emb,
                       embedding <-> %s           AS l2_dist,
                       1 - (embedding <=> %s)     AS cosine_sim
                FROM items
                ORDER BY l2_dist
                LIMIT 3
                """,
                (query, query),
            )
            rows = cur.fetchall()

    print(f"connected : {HOST}:{PORT}/{DBNAME} (pgvector {version})")
    print(f"query     : {query.to_text()}")
    print(f"{'id':>4}  {'embedding':<12}  {'l2_dist':>8}  {'cosine_sim':>10}")
    for row_id, emb, l2, cosine in rows:
        print(f"{row_id:>4}  {emb:<12}  {l2:8.4f}  {cosine:10.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
