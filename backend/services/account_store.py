"""账号与登录会话仓储（SQL 出口；仅服务于认证与会话归属，不承载知识库向量 SQL）。

表职责：
- app_user：本地账号、角色、启用状态；
- auth_session：不透明登录令牌哈希、过期时间与吊销状态。
"""
from __future__ import annotations

import logging
from typing import Optional

import psycopg

from db import get_pool
from errors import BizError, ERR_USER_EXISTS

logger = logging.getLogger("rag.account_store")


async def ensure_auth_schema() -> None:
    """创建认证相关表与索引（幂等；迁移脚本调用，不在业务请求路径执行 DDL）。"""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS app_user (
                    user_id       BIGSERIAL PRIMARY KEY,
                    username      VARCHAR(64)  NOT NULL UNIQUE,
                    display_name  VARCHAR(128) NOT NULL,
                    password_hash VARCHAR(255) NOT NULL,
                    role          VARCHAR(16)  NOT NULL DEFAULT 'member'
                                  CHECK (role IN ('admin', 'member')),
                    status        SMALLINT     NOT NULL DEFAULT 1,
                    created_at    TIMESTAMP    NOT NULL DEFAULT now(),
                    last_login_at TIMESTAMP    NULL
                )
                """
            )
            await cur.execute("CREATE INDEX IF NOT EXISTS idx_app_user_username ON app_user(username)")
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_session (
                    token_hash  CHAR(64)   PRIMARY KEY,
                    user_id     BIGINT      NOT NULL REFERENCES app_user(user_id) ON DELETE CASCADE,
                    expires_at  TIMESTAMP   NOT NULL,
                    revoked_at  TIMESTAMP   NULL,
                    created_at  TIMESTAMP   NOT NULL DEFAULT now(),
                    user_agent  TEXT        NULL,
                    client_ip   VARCHAR(64) NULL
                )
                """
            )
            await cur.execute("CREATE INDEX IF NOT EXISTS idx_auth_session_user ON auth_session(user_id)")
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS operation_log (
                    log_id        BIGSERIAL PRIMARY KEY,
                    actor_user_id BIGINT NULL,
                    action        VARCHAR(64) NOT NULL,
                    target_type   VARCHAR(32) NULL,
                    target_id     VARCHAR(64) NULL,
                    result        SMALLINT    NOT NULL DEFAULT 1,
                    detail        JSONB       NULL,
                    created_at    TIMESTAMP   NOT NULL DEFAULT now()
                )
                """
            )
            await cur.execute("CREATE INDEX IF NOT EXISTS idx_operation_log_actor ON operation_log(actor_user_id)")
            await cur.execute("CREATE INDEX IF NOT EXISTS idx_operation_log_created ON operation_log(created_at DESC)")


async def create_user(
    username: str,
    display_name: str,
    password_hash: str,
    role: str = "member",
) -> int:
    pool = get_pool()
    try:
        async with pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO app_user (username, display_name, password_hash, role)
                        VALUES (%(username)s, %(display_name)s, %(password_hash)s, %(role)s)
                        RETURNING user_id
                        """,
                        {
                            "username": username.strip().lower(),
                            "display_name": display_name.strip() or username,
                            "password_hash": password_hash,
                            "role": role,
                        },
                    )
                    row = await cur.fetchone()
                    return int(row[0])
    except psycopg.errors.UniqueViolation:
        raise BizError(ERR_USER_EXISTS, "用户名已存在", http_status=400) from None


async def get_user_by_username(username: str) -> Optional[dict]:
    return await _get_user("username = %(u)s", {"u": username.strip().lower()})


async def get_credentials_by_username(username: str) -> Optional[dict]:
    """登录专用：只取 user_id 与 password_hash，不返回公开用户资料。"""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT user_id, password_hash
                FROM app_user
                WHERE username = %(u)s
                """,
                {"u": username.strip().lower()},
            )
            row = await cur.fetchone()
            if row is None:
                return None
            return {"user_id": int(row[0]), "password_hash": row[1]}


async def get_user_by_id(user_id: int) -> Optional[dict]:
    return await _get_user("user_id = %(id)s", {"id": user_id})


async def _get_user(where_sql: str, params: dict) -> Optional[dict]:
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT user_id, username, display_name, role, status, created_at, last_login_at
                FROM app_user
                WHERE {where_sql}
                """,
                params,
            )
            row = await cur.fetchone()
            if row is None:
                return None
            cols = ["user_id", "username", "display_name", "role", "status", "created_at", "last_login_at"]
            item = dict(zip(cols, row))
            item["user_id"] = int(item["user_id"])
            item["status"] = int(item["status"])
            return item


async def get_credentials_by_id(user_id: int) -> Optional[dict]:
    """改密专用：按 user_id 取密码哈希（不返回公开资料）。"""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT user_id, password_hash FROM app_user WHERE user_id = %(id)s",
                {"id": int(user_id)},
            )
            row = await cur.fetchone()
            if row is None:
                return None
            return {"user_id": int(row[0]), "password_hash": row[1]}


async def update_password(user_id: int, password_hash: str) -> None:
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE app_user SET password_hash = %(hash)s WHERE user_id = %(id)s",
                    {"hash": password_hash, "id": int(user_id)},
                )


async def touch_last_login(user_id: int) -> None:
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE app_user SET last_login_at = now() WHERE user_id = %(id)s",
                    {"id": user_id},
                )


async def create_auth_session(
    token_hash: str,
    user_id: int,
    ttl_seconds: int,
    user_agent: Optional[str] = None,
    client_ip: Optional[str] = None,
) -> None:
    """写入登录会话；过期时间统一由数据库 now() 计算，避免应用侧 UTC 与库时区混用。"""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO auth_session
                        (token_hash, user_id, expires_at, user_agent, client_ip)
                    VALUES (
                        %(hash)s, %(user_id)s, now() + make_interval(secs => %(ttl)s), %(ua)s, %(ip)s
                    )
                    """,
                    {
                        "hash": token_hash,
                        "user_id": user_id,
                        "ttl": int(ttl_seconds),
                        "ua": user_agent,
                        "ip": client_ip,
                    },
                )


async def get_active_session(token_hash: str, renew_ttl_seconds: int = 0) -> Optional[dict]:
    """校验登录态并（可选）滑动续期。

    renew_ttl_seconds>0 时：剩余有效期不足 TTL 的一半则续到 now()+TTL，
    因此每个会话最多每半个 TTL 写一次库，避免每个请求都产生 UPDATE。
    """
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT u.user_id, u.username, u.display_name, u.role, u.status,
                           s.expires_at, s.token_hash
                    FROM auth_session s
                    JOIN app_user u ON u.user_id = s.user_id
                    WHERE s.token_hash = %(hash)s
                      AND s.revoked_at IS NULL
                      AND s.expires_at > now()
                    """,
                    {"hash": token_hash},
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                item = {
                    "user_id": int(row[0]),
                    "username": row[1],
                    "display_name": row[2],
                    "role": row[3],
                    "status": int(row[4]),
                    "expires_at": row[5],
                }
                if renew_ttl_seconds > 0:
                    # 时间判断与写入都在数据库侧完成，避免应用与库时钟/时区不一致
                    await cur.execute(
                        """
                        UPDATE auth_session
                        SET expires_at = now() + make_interval(secs => %(ttl)s)
                        WHERE token_hash = %(hash)s
                          AND revoked_at IS NULL
                          AND expires_at < now() + make_interval(secs => %(half)s)
                        """,
                        {
                            "ttl": int(renew_ttl_seconds),
                            "half": int(renew_ttl_seconds) // 2,
                            "hash": token_hash,
                        },
                    )
                return item


async def revoke_auth_session(token_hash: str) -> None:
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE auth_session SET revoked_at = now() WHERE token_hash = %(hash)s",
                    {"hash": token_hash},
                )


async def revoke_sessions_for_user(user_id: int, keep_token_hash: Optional[str] = None) -> int:
    """吊销某用户全部登录会话（改密/禁用时使用）；keep_token_hash 用于保留当前会话。"""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE auth_session
                    SET revoked_at = now()
                    WHERE user_id = %(uid)s
                      AND revoked_at IS NULL
                      AND (%(keep)s::text IS NULL OR token_hash <> %(keep)s)
                    """,
                    {"uid": int(user_id), "keep": keep_token_hash},
                )
                return cur.rowcount or 0


async def count_admins() -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM app_user WHERE role = 'admin'")
            row = await cur.fetchone()
            return int(row[0])


async def count_enabled_admins(include_user_id: Optional[int] = None) -> int:
    """统计启用中的 admin；include_user_id 用于“排除目标账号后是否仍有管理员”的判断。"""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            if include_user_id is None:
                await cur.execute(
                    "SELECT count(*) FROM app_user WHERE role = 'admin' AND status = 1"
                )
            else:
                await cur.execute(
                    "SELECT count(*) FROM app_user "
                    "WHERE role = 'admin' AND status = 1 AND user_id <> %(id)s",
                    {"id": int(include_user_id)},
                )
            row = await cur.fetchone()
            return int(row[0])


async def list_users(
    page: int = 1,
    size: int = 20,
    keyword: Optional[str] = None,
    role: Optional[str] = None,
    status: Optional[int] = None,
) -> tuple[int, list[dict]]:
    """管理员用户列表：不含密码哈希；按 user_id 升序；筛选条件全部参数化。"""
    page = max(1, int(page or 1))
    size = max(1, min(int(size or 20), 100))
    offset = (page - 1) * size
    where: list[str] = []
    params: dict = {"lim": size, "off": offset}

    if keyword and keyword.strip():
        where.append("(username ILIKE %(keyword)s OR display_name ILIKE %(keyword)s)")
        params["keyword"] = f"%{keyword.strip()}%"
    if role in ("admin", "member"):
        where.append("role = %(role)s")
        params["role"] = role
    if status is not None:
        where.append("status = %(status)s")
        params["status"] = int(status)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"SELECT count(*) FROM app_user {where_sql}", params)
            total = int((await cur.fetchone())[0])
            await cur.execute(
                f"""
                SELECT user_id, username, display_name, role, status, created_at, last_login_at
                FROM app_user
                {where_sql}
                ORDER BY user_id ASC
                LIMIT %(lim)s OFFSET %(off)s
                """,
                params,
            )
            cols = [c.name for c in cur.description]
            items = [dict(zip(cols, row)) for row in await cur.fetchall()]
            for it in items:
                it["user_id"] = int(it["user_id"])
                it["status"] = int(it["status"])
            return total, items


async def set_user_status(user_id: int, status: int) -> bool:
    """启用/禁用账号；禁用时同步吊销其全部登录态。返回是否存在该用户。"""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE app_user SET status = %(s)s WHERE user_id = %(id)s RETURNING user_id",
                    {"s": int(status), "id": int(user_id)},
                )
                if await cur.fetchone() is None:
                    return False
                if int(status) != 1:
                    await cur.execute(
                        "UPDATE auth_session SET revoked_at = now() "
                        "WHERE user_id = %(id)s AND revoked_at IS NULL",
                        {"id": int(user_id)},
                    )
                return True


async def get_first_admin_id() -> Optional[int]:
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT user_id FROM app_user
                WHERE role = 'admin'
                ORDER BY user_id ASC
                LIMIT 1
                """
            )
            row = await cur.fetchone()
            return int(row[0]) if row else None


async def delete_user(user_id: int) -> None:
    """测试/清理使用：删除测试账号；依赖级联删除其登录会话与私有会话。"""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM app_user WHERE user_id = %(id)s",
                    {"id": int(user_id)},
                )
