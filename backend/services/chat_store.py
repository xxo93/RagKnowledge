"""会话与对话记录仓储。

硬约束（与 vector_store 对齐）：
- chat_session / chat_record 全部 SQL 集中在本文件；routers / services / agent 不得自行拼 SQL；
- 100% 参数化（%(name)s），JSONB 统一经 `::jsonb` 显式转换，杜绝字符串拼接；
- 删除会话为显式事务：先删该会话全部记录，再删会话主表（显式删除风格）；
- 连接获取范围仅限 SQL 执行期间，LLM 流式循环内不得持有连接（db.py）。
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import psycopg

from db import get_pool
from errors import BizError, ERR_PARAM, ERR_SESSION_NOT_FOUND

logger = logging.getLogger("rag.chat_store")

# list_sessions 默认上限，避免一次拉出过多会话
DEFAULT_SESSION_LIMIT = 50


def _json(value) -> Optional[str]:
    """把 dict/list 序列化为 JSONB 文本；空值返回 None（落 NULL）。"""
    if not value:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


# ---------------- chat_session ----------------
async def create_session(user_id: int, session_name: str) -> dict:
    """新建当前用户私有会话；返回 {session_id, session_name, create_time}。"""
    pool = get_pool()
    name = (session_name or "新会话").strip() or "新会话"
    name = name[:128]
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO chat_session (user_id, session_name)
                    VALUES (%(uid)s, %(n)s)
                    RETURNING session_id, session_name, create_time
                    """,
                    {"uid": int(user_id), "n": name},
                )
                row = await cur.fetchone()
                return {"session_id": int(row[0]), "session_name": row[1], "create_time": row[2]}


async def _get_session(session_id: int) -> Optional[dict]:
    """内部存在性校验；不做用户归属判断，供记录落库竞态保护使用。"""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT session_id, session_name, create_time
                FROM chat_session WHERE session_id = %(id)s
                """,
                {"id": session_id},
            )
            row = await cur.fetchone()
            if row is None:
                return None
            return {"session_id": int(row[0]), "session_name": row[1], "create_time": row[2]}


async def get_session_for_user(session_id: int, user_id: int) -> Optional[dict]:
    """返回属于指定用户的会话；越权访问与不存在统一由路由判定为不可见。"""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT s.session_id, s.session_name, s.create_time
                FROM chat_session s
                WHERE s.session_id = %(id)s AND s.user_id = %(uid)s
                """,
                {"id": session_id, "uid": int(user_id)},
            )
            row = await cur.fetchone()
            if row is None:
                return None
            return {"session_id": int(row[0]), "session_name": row[1], "create_time": row[2]}


async def list_sessions(
    user_id: int,
    limit: int = DEFAULT_SESSION_LIMIT,
    offset: int = 0,
    keyword: Optional[str] = None,
) -> tuple[int, list[dict]]:
    """按创建时间（session_id）倒序列出当前用户会话，并冗余统计记录条数。"""
    pool = get_pool()
    limit = max(1, min(int(limit or DEFAULT_SESSION_LIMIT), 500))
    offset = max(0, int(offset or 0))
    keyword = (keyword or "").strip()
    where = ["s.user_id = %(uid)s"]
    params: dict = {"uid": int(user_id), "lim": limit, "off": offset}
    if keyword:
        where.append("s.session_name ILIKE %(kw)s")
        params["kw"] = f"%{keyword}%"
    where_sql = " AND ".join(where)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT count(*) FROM chat_session s WHERE {where_sql}",
                params,
            )
            total = int((await cur.fetchone())[0])
            await cur.execute(
                f"""
                SELECT s.session_id, s.session_name, s.create_time,
                       (SELECT count(*) FROM chat_record r WHERE r.session_id = s.session_id) AS record_count
                FROM chat_session s
                WHERE {where_sql}
                ORDER BY s.session_id DESC
                LIMIT %(lim)s OFFSET %(off)s
                """,
                params,
            )
            cols = [c.name for c in cur.description]
            return total, [dict(zip(cols, row)) for row in await cur.fetchall()]


async def rename_session(session_id: int, user_id: int, session_name: str) -> dict:
    """重命名本人会话；非 owner 或不存在 -> 40402（不泄露他人会话是否存在）。"""
    name = (session_name or "").strip()
    if not name:
        raise BizError(ERR_PARAM, "会话名称不能为空", http_status=400)
    name = name[:128]
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE chat_session
                    SET session_name = %(n)s
                    WHERE session_id = %(id)s AND user_id = %(uid)s
                    RETURNING session_id, session_name, create_time
                    """,
                    {"n": name, "id": int(session_id), "uid": int(user_id)},
                )
                row = await cur.fetchone()
                if row is None:
                    raise BizError(ERR_SESSION_NOT_FOUND, "会话不存在或无权访问", http_status=404)
                return {"session_id": int(row[0]), "session_name": row[1], "create_time": row[2]}


async def count_records(session_id: int) -> int:
    """统计会话现有记录数；供首问自动标题判断幂等条件。"""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM chat_record WHERE session_id = %(sid)s",
                {"sid": int(session_id)},
            )
            return int((await cur.fetchone())[0])


async def maybe_set_auto_title(session_id: int, user_id: int, title: str) -> Optional[str]:
    """首问成功落库后条件更新默认会话名；用户手动改名或已有标题时不覆盖。

    仅当会话名为空或默认名 `新会话` 时更新；重复回调或并发请求由 WHERE 条件保证幂等。
    返回最终会话名；未命中更新条件时返回 None。
    """
    clean = (title or "").strip()
    if not clean:
        return None
    clean = clean[:128]
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE chat_session
                SET session_name = %(n)s
                WHERE session_id = %(id)s
                  AND user_id = %(uid)s
                  AND (session_name IS NULL OR session_name IN ('新会话', ''))
                RETURNING session_name
                """,
                {"n": clean, "id": int(session_id), "uid": int(user_id)},
            )
            row = await cur.fetchone()
            return row[0] if row else None


async def delete_session(session_id: int, user_id: int) -> None:
    """删除当前用户会话及其全部记录；非本人或不存在时返回 40402。

    先按归属条件加锁查询，再删除记录与会话，避免越权请求触达他人数据。
    """
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT session_id FROM chat_session
                    WHERE session_id = %(id)s AND user_id = %(uid)s
                    FOR UPDATE
                    """,
                    {"id": int(session_id), "uid": int(user_id)},
                )
                if await cur.fetchone() is None:
                    raise BizError(ERR_SESSION_NOT_FOUND, "会话不存在或已删除", http_status=404)

                await cur.execute(
                    "DELETE FROM chat_record WHERE session_id = %(id)s", {"id": int(session_id)}
                )
                await cur.execute(
                    "DELETE FROM chat_session WHERE session_id = %(id)s AND user_id = %(uid)s",
                    {"id": int(session_id), "uid": int(user_id)},
                )


# ---------------- chat_record ----------------
async def insert_record(
    session_id: int,
    query: str,
    answer: str,
    tool_call_info: Optional[list[dict]] = None,
    source_ref: Optional[list[dict]] = None,
) -> Optional[int]:
    """落库一条问答；成功返回 record_id，未保存返回 None。

    会话已删除、外键冲突等竞态场景直接返回 None，不向路由层抛出底层数据库异常。
    """
    if await _get_session(session_id) is None:
        logger.warning("会话 %s 已不存在，跳过记录落库（竞态删除）", session_id)
        return None

    pool = get_pool()
    try:
        async with pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO chat_record (session_id, query, answer, tool_call_info, source_ref)
                        VALUES (%(sid)s, %(q)s, %(a)s, %(tc)s::jsonb, %(src)s::jsonb)
                        RETURNING record_id
                        """,
                        {
                            "sid": session_id,
                            "q": query,
                            "a": answer or "",
                            "tc": _json(tool_call_info),
                            "src": _json(source_ref),
                        },
                    )
                    return int((await cur.fetchone())[0])
    except psycopg.errors.ForeignKeyViolation:
        logger.warning("会话 %s 外键冲突，跳过记录落库（竞态删除）", session_id)
        return None


async def list_records(
    session_id: int,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> tuple[int, list[dict]]:
    """按时间正序返回会话历史；不传 limit 时保持全量兼容行为。"""
    pool = get_pool()
    params: dict = {"sid": int(session_id)}
    paging_sql = ""
    if limit is not None:
        limit = max(1, min(int(limit), 100))
        paging_sql = "LIMIT %(lim)s OFFSET %(off)s"
        params["lim"] = limit
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM chat_record WHERE session_id = %(sid)s",
                {"sid": int(session_id)},
            )
            total = int((await cur.fetchone())[0])
            if limit is not None:
                offset = max(0, total - limit if offset is None else int(offset))
                params["off"] = offset
            await cur.execute(
                f"""
                SELECT record_id, query, answer, tool_call_info, source_ref, create_time
                FROM chat_record
                WHERE session_id = %(sid)s
                ORDER BY record_id ASC
                {paging_sql}
                """,
                params,
            )
            cols = [c.name for c in cur.description]
            items = [dict(zip(cols, row)) for row in await cur.fetchall()]
            for it in items:
                it["record_id"] = int(it["record_id"])
                # psycopg 已把 jsonb 解码为 list/dict；这里兜底 None
                it.setdefault("tool_call_info", None)
                it.setdefault("source_ref", None)
            return total, items


async def recent_records(session_id: int, rounds: int) -> list[dict]:
    """供记忆装配：取最近 rounds 条记录（倒序），调用方负责反转与角色映射。"""
    pool = get_pool()
    n = max(1, min(int(rounds or 5), 20))
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT query, answer
                FROM chat_record
                WHERE session_id = %(sid)s
                ORDER BY record_id DESC
                LIMIT %(n)s
                """,
                {"sid": session_id, "n": n},
            )
            return [{"query": r[0], "answer": r[1]} for r in await cur.fetchall()]
