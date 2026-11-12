"""M0 幂等建库：创建 settings.DB_DSN 指向的业务库 rag_agent（不存在才建）。

CREATE DATABASE 不能在普通事务中执行，因此使用 autocommit 独立连接到维护库 postgres。
本脚本只建库、不建表；表/扩展/索引由 scripts/init_db.py 完成。
运行：.venv\\Scripts\\python.exe backend\\scripts\\create_database.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psycopg  # noqa: E402
from backend.config import settings  # noqa: E402


def main() -> int:
    m = re.search(r"/([^/?#]+)(?:\?|$)", settings.DB_DSN)
    dbname = m.group(1) if m else "rag_agent"
    # 标识符白名单，避免把配置串直接拼进 DDL
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", dbname):
        print(f"[错误] 非法库名：{dbname!r}")
        return 1

    admin_dsn = re.sub(r"/[^/?#]+($|\?)", r"/postgres\1", settings.DB_DSN)
    print(f"[连接维护库] {admin_dsn.split('@')[-1]}；目标业务库 = {dbname}")
    try:
        conn = psycopg.connect(admin_dsn, autocommit=True, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001
        print("[连接失败]", type(exc).__name__, str(exc).strip().splitlines()[-1])
        return 1

    try:
        cur = conn.cursor()
        exists = cur.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
        ).fetchone()
        if exists:
            print(f"[跳过] 数据库 {dbname} 已存在")
            return 0
        # 模板0 + UTF8，避免集群默认 locale 对后续对象的潜在限制
        cur.execute(
            f'CREATE DATABASE "{dbname}" ENCODING \'UTF8\' TEMPLATE template0'
        )
        print(f"[完成] 数据库 {dbname} 已创建（UTF8）")
        return 0
    except Exception as exc:  # noqa: BLE001
        print("[建库失败]", type(exc).__name__, str(exc).strip().splitlines()[-1])
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
