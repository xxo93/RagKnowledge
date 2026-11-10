"""PostgreSQL + pgvector 异步连接池。

要点：
1. 使用 psycopg3 异步连接池 AsyncConnectionPool，匹配 FastAPI 全异步；
2. 建池后必须 register_vector，psycopg 才知道 vector 列的编解码；
3. 【ADR-006 实机教训】pgvector-python 0.5.x 的 dumper 只注册了 Vector 与 numpy.ndarray，
直接传 Python list 会被推断为 smallint[] 而报类型错误；统一由 to_vector() 包装；
4. 骨架阶段：数据库不可用时不让服务崩溃，db_ready=False，需要 DB 的业务接口统一返回 50001；
5. 禁止在 LLM 流式循环内持有连接，连接获取范围仅限 SQL 执行期间。
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

import psycopg
from psycopg_pool import AsyncConnectionPool
from pgvector.psycopg import register_vector_async
from pgvector import Vector

from config import settings
from errors import BizError, ERR_DB_UNAVAILABLE

logger = logging.getLogger("rag.db")

_pool: Optional[AsyncConnectionPool] = None
db_ready: bool = False


def is_ready() -> bool:
    """动态读取当前就绪状态（避免调用方用 `from db import db_ready` 取到布尔值快照）。"""
    return db_ready


def to_vector(values: Iterable[float] | Vector) -> Vector:
    """统一包装向量输入；已是 pgvector.Vector 时保持不变。"""
    if isinstance(values, Vector):
        return values
    return Vector(list(values))


async def _configure_pool_conn(conn) -> None:
    """异步连接建立后注册 pgvector 类型；AsyncConnectionPool 会 await 本协程。"""
    await register_vector_async(conn)


async def init_pool() -> bool:
    """启动时探测数据库并创建连接池；失败返回 False 且保持降级状态。

    先使用短超时一次性连接探测，再创建按需连接的连接池；业务层在未就绪时统一抛 503。
    """
    global _pool, db_ready
    db_ready = False
    try:
        probe = await psycopg.AsyncConnection.connect(
            settings.DB_DSN, autocommit=True, connect_timeout=3
        )
        await register_vector_async(probe)
        async with probe.cursor() as cur:
            await cur.execute("SELECT 1")
            await cur.fetchone()
        await probe.close()

        _pool = AsyncConnectionPool(
            settings.DB_DSN,
            min_size=0,
            max_size=settings.DB_POOL_MAX,
            open=False,
            configure=_configure_pool_conn,
            kwargs={"autocommit": False, "connect_timeout": 3},
        )
        await _pool.open()
        db_ready = True
        logger.info("数据库连接池就绪：max=%d（按需建连）", settings.DB_POOL_MAX)
        return True
    except Exception as exc:  # noqa: BLE001 - 数据库不可用时统一降级，避免启动即退出
        _pool = None
        db_ready = False
        logger.warning("数据库不可用，业务接口将返回服务暂不可用（503）：%s", exc)
        return False


async def close_pool() -> None:
    """应用 lifespan 关闭时调用，优雅归还所有连接。"""
    global _pool, db_ready
    if _pool is not None:
        await _pool.close()
        _pool = None
    db_ready = False


def get_pool() -> AsyncConnectionPool:
    """业务层获取连接池；未就绪时抛出统一业务错误（50001），不在各调用点重复判断。"""
    if _pool is None or not db_ready:
        raise BizError(ERR_DB_UNAVAILABLE, "数据库连接暂不可用，请稍后重试", http_status=503)
    return _pool


async def extension_version() -> Optional[str]:
    """读取 pgvector 扩展版本；数据库不可用或查询失败时返回 None。"""
    if not db_ready:
        return None
    pool = get_pool()
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
                )
                row = await cur.fetchone()
                return row[0] if row else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 pgvector 版本失败：%s", exc)
        return None
