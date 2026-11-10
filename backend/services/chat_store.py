"""会话与对话记录仓储（F-10/F-11 / 架构 §3.4 / ADR-002、R2）。

硬约束（与 vector_store 对齐）：
- chat_session / chat_record 全部 SQL 集中在本文件；routers / services / agent 不得自行拼 SQL；
- 100% 参数化（%(name)s），JSONB 统一经 `::jsonb` 显式转换，杜绝字符串拼接；
- 删除会话为显式事务：先删该会话全部记录，再删会话主表（架构 §3.4 显式删除风格）；
- 连接获取范围仅限 SQL 执行期间，LLM 流式循环内不得持有连接（db.py §5.5）。
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


async def list_sessions(user_id: int, limit: int = DEFAULT_SESSION_LIMIT) -> list[dict]:
    """按创建时间（session_id）倒序列出当前用户会话，并冗余统计记录条数。"""
    pool = get_pool()
    limit = max(1, min(int(limit or DEFAULT_SESSION_LIMIT), 500))
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT s.session_id, s.session_name, s.create_time,
                       (SELECT count(*) FROM chat_record r WHERE r.session_id = s.session_id) AS record_count
                FROM chat_session s
                WHERE s.user_id = %(uid)s
                ORDER BY s.session_id DESC
                LIMIT %(lim)s
                """,
                {"uid": int(user_id), "lim": limit},
            )
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, row)) for row in await cur.fetchall()]


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


async def delete_session(session_id: int, user_id: int) -> None:
    """删除用户本人会话及其全部记录（单事务）；非 owner 或不存在 -> 40402。

    先按 owner 锁定会话再删记录：若直接按 session_id 删记录，越权请求会在返回 404
    之前就把他人的聊天记录清空（跨用户数据销毁）。
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

    竞态加固（S2）：流前校验与会话落库之间存在窄窗口，若会话在此期间被删除，
    前置存在性校验不通过则直接返回 None、不落库不抛错；外键冲突同样降级为 None，
    由路由在 done 帧以 saved=false 告知前端，避免裸 FK 异常堆栈与脏写。
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


async def list_records(session_id: int) -> list[dict]:
    """历史回填：按 record_id 正序返回，含来源与工具信息（解析 JSONB）。"""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT record_id, query, answer, tool_call_info, source_ref, create_time
                FROM chat_record
                WHERE session_id = %(sid)s
                ORDER BY record_id ASC
                """,
                {"sid": session_id},
            )
            cols = [c.name for c in cur.description]
            items = [dict(zip(cols, row)) for row in await cur.fetchall()]
            for it in items:
                it["record_id"] = int(it["record_id"])
                # psycopg 已把 jsonb 解码为 list/dict；这里兜底 None
                it.setdefault("tool_call_info", None)
                it.setdefault("source_ref", None)
            return items


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
