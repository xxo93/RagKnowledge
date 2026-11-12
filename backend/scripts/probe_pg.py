"""M0 只读侦察：连接 PostgreSQL 维护库 postgres，评估建库与扩展前置条件。

不创建任何对象。使用同步 psycopg，规避 Windows 事件循环限制。
运行：.venv\\Scripts\\python.exe backend\\scripts\\probe_pg.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psycopg  # noqa: E402
from backend.config import settings  # noqa: E402


def main() -> int:
    # 把 DSN 末尾库名替换为维护库 postgres（其余用户/密码/主机/端口不变）
    admin_dsn = re.sub(r"/[^/?#]+($|\?)", r"/postgres\1", settings.DB_DSN)
    print("target =", admin_dsn.split("@")[-1])
    try:
        conn = psycopg.connect(admin_dsn, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001
        print("ADMIN_CONNECT_FAIL:", type(exc).__name__, str(exc).strip().splitlines()[-1])
        return 1

    cur = conn.cursor()
    try:
        ver = cur.execute("SELECT version()").fetchone()[0]
        print("server =", ver.split(",")[0].strip())
        user = cur.execute("SELECT current_user").fetchone()[0]
        row = cur.execute(
            "SELECT usesuper, usecreatedb, userepl FROM pg_user WHERE usename = current_user"
        ).fetchone()
        print(f"user = {user} | superuser={row[0]} createdb={row[1]}")

        dbs = [
            r[0]
            for r in cur.execute(
                "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY 1"
            )
        ]
        print("databases =", dbs)
        print("rag_agent exists =", "rag_agent" in dbs)

        for name in ("vector", "pg_trgm"):
            ext = cur.execute(
                "SELECT default_version, installed_version "
                "FROM pg_available_extensions WHERE name = %s",
                (name,),
            ).fetchone()
            if ext is None:
                print(f"extension {name}: NOT_AVAILABLE（服务器未安装该扩展控制文件）")
            else:
                print(
                    f"extension {name}: available, default_version={ext[0]}, "
                    f"installed_in_postgres_db={ext[1]}"
                )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
