"""数据库初始化程序（架构 §12 启动序列第 1 步 / §5.4 维度治理）。

用法（在 backend/ 目录下）：
    python -m scripts.init_db
或：
    python scripts/init_db.py

职责：
1. 读取 init_db.sql，把占位符 __EMBEDDING_DIM__ 替换为配置 EMBEDDING_DIM；
2. 幂等执行（IF NOT EXISTS）：扩展、四张表、两个普通索引、一个 HNSW 索引；
3. 执行后回读校验：pgvector 版本、embedding 列类型、HNSW 索引是否存在；
4. 连接失败或列维度不符时打印可直接照做的修复指引，而不是抛堆栈。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 兼容直接 `python scripts/init_db.py` 的运行方式：把 backend 目录加入 sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import psycopg  # noqa: E402

from config import settings  # noqa: E402

SQL_FILE = BACKEND_DIR / "init_db.sql"


def _split_statements(sql: str) -> list[str]:
    """按分号拆分 DDL；本项目 DDL 无存储过程体，简单拆分即可。"""
    return [s.strip() for s in sql.split(";") if s.strip()]


def verify(cur) -> list[str]:
    """回读校验，返回提示信息列表。"""
    msgs: list[str] = []

    cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    row = cur.fetchone()
    msgs.append(f"pgvector 扩展版本：{row[0] if row else '【缺失】请先安装扩展并 CREATE EXTENSION vector'}")

    cur.execute(
        """
        SELECT format_type(atttypid, atttypmod)
        FROM pg_attribute
        WHERE attrelid = 'document_chunk'::regclass
          AND attname = 'embedding' AND NOT attisdropped
        """
    )
    row = cur.fetchone()
    actual = row[0] if row else "【列不存在】"
    expect = f"vector({settings.EMBEDDING_DIM})"
    ok_dim = actual == expect
    msgs.append(f"embedding 列类型：{actual}（期望 {expect}）" + (" ✔" if ok_dim else " ✘ 维度不符"))
    if not ok_dim:
        msgs.append(
            "  修复：ALTER TABLE document_chunk ALTER COLUMN embedding TYPE vector(%d); "
            "并对存量切片重新向量化（换模型必须重建）。" % settings.EMBEDDING_DIM
        )

    cur.execute(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'document_chunk' AND indexname = 'idx_chunk_embedding'"
    )
    row = cur.fetchone()
    msgs.append(f"HNSW 索引 idx_chunk_embedding：{'存在 ✔' if row else '【缺失，见上方 DDL 自动创建结果】'}")
    return msgs


def main() -> int:
    if not SQL_FILE.exists():
        print(f"[错误] 找不到 DDL 文件：{SQL_FILE}")
        return 1

    raw = SQL_FILE.read_text(encoding="utf-8")
    ddl = raw.replace("__EMBEDDING_DIM__", str(settings.EMBEDDING_DIM))

    print(f"[连接] {settings.DB_DSN}")
    print(f"[配置] EMBEDDING_DIM={settings.EMBEDDING_DIM}")
    try:
        conn = psycopg.connect(settings.DB_DSN)
    except psycopg.OperationalError as exc:
        print("[连接失败] 请检查：")
        print("  1. PostgreSQL 服务是否运行（Windows: Get-Service postgresql-x64-17）；")
        print("  2. 端口 5432、库名 rag_agent、用户名密码是否与 .env 的 DB_DSN 一致；")
        print("  3. 库不存在时先执行：CREATE DATABASE rag_agent;")
        print(f"  原始错误：{exc}")
        return 1

    with conn:
        with conn.cursor() as cur:
            for stmt in _split_statements(ddl):
                cur.execute(stmt)
            print("[执行] DDL 语句已执行（幂等）")
            for msg in verify(cur):
                print("  - " + msg)
    print("[完成] 数据库初始化结束。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
