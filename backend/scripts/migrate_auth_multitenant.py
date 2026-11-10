"""幂等迁移：多用户认证、会话归属、审计表与初始管理员。

用途：
1. 创建 app_user / auth_session / operation_log；
2. 为 chat_session 增加 user_id、索引与外键，回填后补 NOT NULL；
3. 初始化 admin 账号；环境变量 SEED_ADMIN_PASSWORD 可指定初始密码；
4. 将历史无归属会话回填到初始 admin；
5. 输出迁移前后表计数，便于回滚核对。

运行方式（在 backend/ 目录或任意目录均可，脚本自动加入 backend 路径）：
    python scripts/migrate_auth_multitenant.py
"""
from __future__ import annotations

import asyncio
import secrets
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from config import settings  # noqa: E402
from db import close_pool, init_pool  # noqa: E402
from services import account_store, security  # noqa: E402


async def main() -> int:
    if not await init_pool():
        print("MIGRATION_RESULT=DB_UNAVAILABLE")
        return 1

    try:
        await account_store.ensure_auth_schema()
        pool = account_store.get_pool()

        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS user_id BIGINT")
                await cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_chat_session_user_time
                    ON chat_session (user_id, session_id DESC)
                    """
                )
                await cur.execute(
                    """
                    SELECT 1 FROM pg_constraint WHERE conname = 'fk_chat_session_user'
                    """
                )
                if await cur.fetchone() is None:
                    await cur.execute(
                        """
                        ALTER TABLE chat_session
                        ADD CONSTRAINT fk_chat_session_user
                        FOREIGN KEY (user_id) REFERENCES app_user(user_id) ON DELETE CASCADE
                        """
                    )

        admin_id = await account_store.get_first_admin_id()
        if admin_id is None:
            # 账号/密码来自 config.py 的 DEFAULT_ADMIN_*（可用 .env 或环境变量覆盖）；
            # 密码置空则随机生成一次性临时密码。
            username = (settings.DEFAULT_ADMIN_USERNAME or "admin").strip().lower()
            display = (settings.DEFAULT_ADMIN_DISPLAY_NAME or "系统管理员").strip()
            password = (settings.DEFAULT_ADMIN_PASSWORD or "").strip()
            generated = False
            if not password:
                password = secrets.token_urlsafe(12)
                generated = True
            await account_store.create_user(
                username, display, security.hash_password(password), "admin"
            )
            admin_id = await account_store.get_first_admin_id()
            print("SEED_ADMIN_CREATED=YES username=%s" % username)
            if generated:
                print("SEED_ADMIN_TEMP_PASSWORD=%s" % password)
                print("提示：请立即保存该临时密码；后续可通过 scripts/ensure_admin.py --reset-password 重置。")
            else:
                print("SEED_ADMIN_PASSWORD_SOURCE=DEFAULT_ADMIN_PASSWORD")

        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE chat_session SET user_id = %(admin_id)s WHERE user_id IS NULL",
                    {"admin_id": admin_id},
                )
                backfilled = cur.rowcount or 0

        # 回填完成后补 NOT NULL：先校验确无 NULL，避免把脏数据卡成不可用
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT count(*) FROM chat_session WHERE user_id IS NULL")
                null_rows = int((await cur.fetchone())[0])
                if null_rows == 0:
                    await cur.execute(
                        "ALTER TABLE chat_session ALTER COLUMN user_id SET NOT NULL"
                    )
                    print("CHAT_SESSION_USER_ID_NOT_NULL=YES")
                else:
                    print("CHAT_SESSION_USER_ID_NOT_NULL=SKIP null_rows=%s" % null_rows)

                counts = {}
                for table in ("app_user", "auth_session", "chat_session", "chat_record",
                              "document", "document_chunk", "operation_log"):
                    await cur.execute("SELECT count(*) FROM %s" % table)
                    counts[table] = int((await cur.fetchone())[0])
                print("TABLE_COUNTS=" + " ".join(f"{k}:{v}" for k, v in counts.items()))

        print("MIGRATION_RESULT=OK admin_id=%s legacy_backfilled=%s" % (admin_id, backfilled))
        return 0
    finally:
        await close_pool()


if __name__ == "__main__":
    if sys.platform == "win32":
        # Windows 默认 ProactorEventLoop 与异步 psycopg 不兼容；历史启动脚本统一使用 SelectorEventLoop。
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(asyncio.run(main()))
