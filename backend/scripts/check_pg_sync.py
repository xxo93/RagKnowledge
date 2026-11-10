"""同步探测本机 PostgreSQL + pgvector 是否可达（M0 环境核对用）。

使用同步 psycopg，避免 Windows ProactorEventLoop 对异步驱动的限制；
只做只读探测，不建库、不改数据。DSN 取自 backend/config.py（即 .env / 默认值）。
运行：.venv\\Scripts\\python.exe backend\\scripts\\check_pg_sync.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402
from config import settings  # noqa: E402


def main() -> int:
    dsn = settings.DB_DSN
    # 仅打印主机/库名，隐藏用户名密码
    safe = dsn.split("@")[-1] if "@" in dsn else dsn
    print("target =", safe)
    try:
        conn = psycopg.connect(dsn, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001
        print("CONNECT_FAIL:", type(exc).__name__, str(exc).strip().splitlines()[-1])
        return 1

    try:
        db, ver = conn.execute("SELECT current_database(), version()").fetchone()
        print("CONNECT_OK database =", db)
        print("server =", ver.split(",")[0])
        row = conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
        print("pgvector_extension =", row[0] if row else "NOT_INSTALLED")
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1"
            )
        ]
        print("public_tables =", tables if tables else "(empty)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
