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
from datetime import datetime
from typing import Optional

import psycopg

from ..db import get_pool
from ..errors import BizError, ERR_PARAM, ERR_SESSION_NOT_FOUND
from . import evidence as evidence_svc

logger = logging.getLogger("rag.chat_store")

# list_sessions 默认上限，避免一次拉出过多会话
DEFAULT_SESSION_LIMIT = 50


def _json(value) -> Optional[str]:
    """把 dict/list 序列化为 JSONB 文本；空值返回 None（落 NULL）。"""
    if not value:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


# ---------------- 结构升级 ----------------
async def ensure_record_schema() -> None:
    """启动时补齐 chat_record / chat_session 的新列（幂等）；失败只记 warning，不影响服务启动。

    老库没有 table_payload 时，历史回放只能显示文字汇总；补列后新记录可还原表格卡。
    v3 记忆字段（answer_digest / token 用量 / summary 水位 / 临时会话开关）同样在此补齐，
    避免要求运维手工执行迁移。
    """
    try:
        pool = get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("ALTER TABLE chat_record ADD COLUMN IF NOT EXISTS table_payload JSONB DEFAULT NULL")
                await conn.execute("ALTER TABLE chat_record ADD COLUMN IF NOT EXISTS answer_digest TEXT DEFAULT NULL")
                await conn.execute("ALTER TABLE chat_record ADD COLUMN IF NOT EXISTS prompt_tokens INT DEFAULT NULL")
                await conn.execute("ALTER TABLE chat_record ADD COLUMN IF NOT EXISTS completion_tokens INT DEFAULT NULL")
                await conn.execute("ALTER TABLE chat_record ADD COLUMN IF NOT EXISTS intent VARCHAR(32) DEFAULT NULL")
                # v3 / WP4：记录该轮关联的工作流任务，历史回放可重新拉取 PlanCard 快照
                await conn.execute("ALTER TABLE chat_record ADD COLUMN IF NOT EXISTS workflow_task_id BIGINT DEFAULT NULL")
                await conn.execute("ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS summary TEXT DEFAULT NULL")
                await conn.execute(
                    "ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS summarized_until_record_id BIGINT DEFAULT NULL"
                )
                await conn.execute(
                    "ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS memory_enabled BOOLEAN NOT NULL DEFAULT TRUE"
                )
                # v3 / M8 WP5-9：置顶与归档；last_active_at 用于「长期不用自动归档」判定，
                # 历史会话按最后一条记录时间回填，没有记录则用创建时间。
                await conn.execute(
                    "ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS is_pinned BOOLEAN NOT NULL DEFAULT FALSE"
                )
                await conn.execute("ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP DEFAULT NULL")
                await conn.execute("ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS last_active_at TIMESTAMP DEFAULT NULL")
                await conn.execute(
                    """
                    UPDATE chat_session s
                    SET last_active_at = COALESCE(
                        (SELECT max(r.create_time) FROM chat_record r WHERE r.session_id = s.session_id),
                        s.create_time
                    )
                    WHERE s.last_active_at IS NULL
                    """
                )
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_chat_session_active
                    ON chat_session (user_id, archived_at, is_pinned, last_active_at DESC)
                    """
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("chat_record/chat_session 补列失败：%s", type(exc).__name__)


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
                    INSERT INTO chat_session (user_id, session_name, last_active_at)
                    VALUES (%(uid)s, %(n)s, now())
                    RETURNING session_id, session_name, create_time, last_active_at
                    """,
                    {"uid": int(user_id), "n": name},
                )
                row = await cur.fetchone()
                return {
                    "session_id": int(row[0]),
                    "session_name": row[1],
                    "create_time": row[2],
                    "last_active_at": row[3],
                    "is_pinned": False,
                    "archived_at": None,
                }


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
    archived: bool = False,
    cursor_last_active_at: Optional[datetime] = None,
    cursor_session_id: Optional[int] = None,
    cursor_is_pinned: Optional[bool] = None,
) -> tuple[int, list[dict], int, Optional[dict]]:
    """列出当前用户会话，返回 (总数, 会话, 归档总数, next_cursor)。

    排序：置顶优先 -> 最近活跃优先；归档列表按归档时间倒序。
    v4 / N1：主列表支持 keyset 游标（is_pinned + active + session_id 复合键），
    传入游标时忽略 offset；page/size 调用方式保持兼容。
    """
    pool = get_pool()
    limit = max(1, min(int(limit or DEFAULT_SESSION_LIMIT), 500))
    offset = max(0, int(offset or 0))
    keyword = (keyword or "").strip()
    base_where = ["s.user_id = %(uid)s"]
    params: dict = {"uid": int(user_id), "lim": limit + 1, "off": offset}
    base_where.append("s.archived_at IS NOT NULL" if archived else "s.archived_at IS NULL")
    if keyword:
        base_where.append("s.session_name ILIKE %(kw)s")
        params["kw"] = f"%{keyword}%"
    base_where_sql = " AND ".join(base_where)

    where_sql = base_where_sql
    use_cursor = bool(
        cursor_last_active_at is not None
        and cursor_session_id is not None
        and not archived
    )
    if use_cursor:
        # 与 ORDER BY 完全一致的复合键：布尔、活跃时间、会话 ID 均为降序
        where_sql += (
            " AND (s.is_pinned, COALESCE(s.last_active_at, s.create_time), s.session_id)"
            " < (%(cur_pinned)s, %(cur_active)s, %(cur_sid)s)"
        )
        params["cur_pinned"] = bool(cursor_is_pinned)
        params["cur_active"] = cursor_last_active_at
        params["cur_sid"] = int(cursor_session_id)

    order_sql = (
        "s.archived_at DESC, s.session_id DESC"
        if archived
        else "s.is_pinned DESC, COALESCE(s.last_active_at, s.create_time) DESC, s.session_id DESC"
    )
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            archived_where = "s.user_id = %(uid)s AND s.archived_at IS NOT NULL"
            if keyword:
                archived_where += " AND s.session_name ILIKE %(kw)s"
            await cur.execute(
                f"SELECT count(*) FROM chat_session s WHERE {archived_where}",
                params,
            )
            archived_total = int((await cur.fetchone())[0])
            # 总数不受游标影响，供前端展示“已加载 / 总数”
            await cur.execute(
                f"SELECT count(*) FROM chat_session s WHERE {base_where_sql}",
                params,
            )
            total = int((await cur.fetchone())[0])
            await cur.execute(
                f"""
                SELECT s.session_id, s.session_name, s.create_time, s.last_active_at,
                       s.is_pinned, s.archived_at,
                       (SELECT count(*) FROM chat_record r WHERE r.session_id = s.session_id) AS record_count
                FROM chat_session s
                WHERE {where_sql}
                ORDER BY {order_sql}
                LIMIT %(lim)s OFFSET %(off)s
                """,
                params,
            )
            cols = [c.name for c in cur.description]
            fetched = [dict(zip(cols, row)) for row in await cur.fetchall()]
            # 多取一条用于判断是否还有下一页，避免最后一页仍返回 next_cursor
            has_more = len(fetched) > limit
            items = fetched[:limit]

    next_cursor = None
    if items and not archived and has_more:
        last = items[-1]
        active = last.get("last_active_at") or last.get("create_time")
        if active is not None:
            next_cursor = {
                "last_active_at": active.isoformat(sep=" ") if hasattr(active, "isoformat") else str(active),
                "session_id": last["session_id"],
                "is_pinned": bool(last.get("is_pinned")),
            }
    return total, items, archived_total, next_cursor



async def count_sessions(user_id: int, archived: bool = False, keyword: Optional[str] = None) -> int:
    """统计当前用户主列表/归档列表会话数；keyword 与列表筛选口径一致。"""
    pool = get_pool()
    arch_sql = "s.archived_at IS NOT NULL" if archived else "s.archived_at IS NULL"
    keyword = (keyword or "").strip()
    params: dict = {"uid": int(user_id)}
    kw_sql = ""
    if keyword:
        kw_sql = " AND s.session_name ILIKE %(kw)s"
        params["kw"] = f"%{keyword}%"
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT count(*) FROM chat_session s WHERE s.user_id = %(uid)s AND {arch_sql}{kw_sql}",
                params,
            )
            return int((await cur.fetchone())[0])


async def archive_stale_sessions(user_id: int, days: int) -> int:
    """把超过 N 天没有新消息的会话自动归档；置顶会话不归档，返回本次归档条数。"""
    days = int(days or 0)
    if days <= 0:
        return 0
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE chat_session s
                SET archived_at = now(), is_pinned = FALSE
                WHERE s.user_id = %(uid)s
                  AND s.archived_at IS NULL
                  AND s.is_pinned = FALSE
                  AND COALESCE(s.last_active_at, s.create_time) < now() - make_interval(days => %(days)s)
                RETURNING s.session_id
                """,
                {"uid": int(user_id), "days": days},
            )
            return len(await cur.fetchall())


async def set_session_pinned(session_id: int, user_id: int, pinned: bool) -> dict:
    """置顶/取消置顶本人活跃会话；归档会话不可置顶（返回 40402）。"""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE chat_session
                    SET is_pinned = %(p)s
                    WHERE session_id = %(id)s AND user_id = %(uid)s AND archived_at IS NULL
                    RETURNING session_id, session_name, create_time, last_active_at, is_pinned, archived_at
                    """,
                    {"p": bool(pinned), "id": int(session_id), "uid": int(user_id)},
                )
                row = await cur.fetchone()
                if row is None:
                    raise BizError(ERR_SESSION_NOT_FOUND, "会话不存在或无权访问", http_status=404)
                cols = [c.name for c in cur.description]
                return dict(zip(cols, row))


async def set_session_archived(session_id: int, user_id: int, archived: bool) -> dict:
    """归档/恢复本人会话；归档时自动取消置顶，恢复时刷新活跃时间。"""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                if archived:
                    await cur.execute(
                        """
                        UPDATE chat_session
                        SET archived_at = now(), is_pinned = FALSE
                        WHERE session_id = %(id)s AND user_id = %(uid)s AND archived_at IS NULL
                        RETURNING session_id, session_name, create_time, last_active_at, is_pinned, archived_at
                        """,
                        {"id": int(session_id), "uid": int(user_id)},
                    )
                else:
                    await cur.execute(
                        """
                        UPDATE chat_session
                        SET archived_at = NULL, last_active_at = now()
                        WHERE session_id = %(id)s AND user_id = %(uid)s AND archived_at IS NOT NULL
                        RETURNING session_id, session_name, create_time, last_active_at, is_pinned, archived_at
                        """,
                        {"id": int(session_id), "uid": int(user_id)},
                    )
                row = await cur.fetchone()
                if row is None:
                    raise BizError(ERR_SESSION_NOT_FOUND, "会话不存在或无权访问", http_status=404)
                cols = [c.name for c in cur.description]
                return dict(zip(cols, row))


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
    answer_mode: str = "knowledge",
    table_payload: Optional[dict] = None,
    answer_digest: Optional[str] = None,
    intent: Optional[str] = None,
    workflow_task_id: Optional[int] = None,
) -> Optional[int]:
    """落库一条问答；成功返回 record_id，未保存返回 None。

    answer_mode 记录本条回答用了哪条能力链路（knowledge / web / fallback_* / tool / chat / biz_query），
    历史回放时前端据此还原"这条是不是内部知识库答的"。
    table_payload 保存业务库查询的表格帧（列定义 + 已脱敏行 + 下载链接信息），
    刷新/切页后历史消息仍能还原表格卡，而不是只剩一句文字汇总。
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
                        INSERT INTO chat_record
                            (session_id, query, answer, tool_call_info, source_ref,
                             answer_mode, table_payload, answer_digest, intent, workflow_task_id)
                        VALUES
                            (%(sid)s, %(q)s, %(a)s, %(tc)s::jsonb, %(src)s::jsonb,
                             %(mode)s, %(tbl)s::jsonb, %(digest)s, %(intent)s, %(wfid)s)
                        RETURNING record_id
                        """,
                        {
                            "sid": session_id,
                            "q": query,
                            "a": answer or "",
                            "tc": _json(tool_call_info),
                            "src": _json(source_ref),
                            "mode": answer_mode or "knowledge",
                            "tbl": _json(table_payload),
                            "digest": (answer_digest or None),
                            "intent": (intent or None),
                            "wfid": int(workflow_task_id) if workflow_task_id else None,
                        },
                    )
                    record_id = int((await cur.fetchone())[0])
                    # 有新消息即视为活跃：刷新活跃时间，并让归档会话自动回到主列表
                    await cur.execute(
                        """
                        UPDATE chat_session
                        SET last_active_at = now(), archived_at = NULL
                        WHERE session_id = %(sid)s
                        """,
                        {"sid": int(session_id)},
                    )
                    return record_id
    except psycopg.errors.ForeignKeyViolation:
        logger.warning("会话 %s 外键冲突，跳过记录落库（竞态删除）", session_id)
        return None


async def get_record_for_user(record_id: int, user_id: int) -> Optional[dict]:
    """取一条属于当前用户的问答（反馈、导出等用户侧操作前做归属校验）。"""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT cr.record_id, cr.session_id, cr.query, cr.answer, cr.tool_call_info,
                       cr.source_ref, cr.intent, cr.answer_mode, cr.table_payload,
                       (SELECT l.trace_id FROM chat_io_log l
                        WHERE l.record_id = cr.record_id
                        ORDER BY l.log_id DESC LIMIT 1) AS trace_id,
                       cr.create_time
                FROM chat_record cr
                JOIN chat_session s ON s.session_id = cr.session_id
                WHERE cr.record_id = %(rid)s AND s.user_id = %(uid)s
                """,
                {"rid": int(record_id), "uid": int(user_id)},
            )
            row = await cur.fetchone()
            if row is None:
                return None
            return {
                "record_id": int(row[0]),
                "session_id": int(row[1]),
                "query": row[2],
                "answer": row[3],
                "tool_call_info": row[4],
                "source_ref": row[5] or [],
                "intent": row[6],
                "answer_mode": row[7],
                "table_payload": row[8],
                "trace_id": row[9],
                "create_time": row[10].isoformat() if row[10] else None,
            }


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
                SELECT cr.record_id, cr.query, cr.answer, cr.tool_call_info, cr.source_ref, cr.create_time,
                       cr.answer_mode, cr.table_payload, cr.intent, cr.workflow_task_id,
                       (SELECT l.trace_id FROM chat_io_log l
                        WHERE l.record_id = cr.record_id
                        ORDER BY l.log_id DESC LIMIT 1) AS trace_id,
                       (SELECT f.rating FROM chat_feedback f
                        WHERE f.record_id = cr.record_id
                        ORDER BY f.feedback_id DESC LIMIT 1) AS feedback_rating
                FROM chat_record cr
                WHERE cr.session_id = %(sid)s
                ORDER BY cr.record_id ASC
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
                it.setdefault("table_payload", None)
                it.setdefault("intent", None)
                it.setdefault("workflow_task_id", None)
                if it.get("workflow_task_id") is not None:
                    it["workflow_task_id"] = int(it["workflow_task_id"])
                # WP5-10：历史回放也带依据强度，口径与当轮 done 帧完全一致
                src = it.get("source_ref") or []
                it["evidence"] = evidence_svc.assess(
                    it.get("answer_mode"),
                    len(src) if isinstance(src, list) else 0,
                    has_table=bool(it.get("table_payload")),
                )
            return total, items


async def get_last_turn(session_id: int) -> dict | None:
    """取最近一轮的 intent / answer_mode / query，用于上下文延续与"上轮知识库追问"判断。

    历史记录没有 intent 列（v3 之前）时，用 answer_mode 反推意图，保证老会话也有上下文延续。
    """
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT record_id, query, intent, answer_mode
                FROM chat_record
                WHERE session_id = %(sid)s
                ORDER BY record_id DESC
                LIMIT 1
                """,
                {"sid": int(session_id)},
            )
            row = await cur.fetchone()
    if not row:
        return None
    record_id, query, intent, answer_mode = row
    if not intent:
        intent = {
            "knowledge": "knowledge",
            "web": "knowledge",
            "fallback_guide": "knowledge",
            "fallback_contact": "knowledge",
            "tool": "calculator",
            "biz_query": "biz_query",
            "general": "general",
            "chat": "general",
        }.get(answer_mode, "knowledge")
    return {"query": query or "", "intent": intent, "answer_mode": answer_mode, "record_id": record_id}


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


# ---------------- 会话记忆（v3 / WP1）----------------
async def get_memory_state(session_id: int, user_id: int) -> Optional[dict]:
    """读取会话记忆元信息；越权/不存在返回 None（由路由统一映射 40402）。"""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT session_id, session_name, summary, summarized_until_record_id, memory_enabled
                FROM chat_session
                WHERE session_id = %(id)s AND user_id = %(uid)s
                """,
                {"id": int(session_id), "uid": int(user_id)},
            )
            row = await cur.fetchone()
            if row is None:
                return None
            return {
                "session_id": int(row[0]),
                "session_name": row[1],
                "summary": row[2] or "",
                "summarized_until_record_id": int(row[3]) if row[3] is not None else 0,
                "memory_enabled": bool(row[4]),
            }


async def context_records(session_id: int, limit: int) -> list[dict]:
    """按 record_id 倒序取最近 limit 条记录（含 digest 与答案全文），供窗口装配。"""
    pool = get_pool()
    n = max(1, min(int(limit or 8), 60))
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT record_id, query, answer, answer_digest, prompt_tokens, completion_tokens
                FROM chat_record
                WHERE session_id = %(sid)s
                ORDER BY record_id DESC
                LIMIT %(n)s
                """,
                {"sid": int(session_id), "n": n},
            )
            cols = [c.name for c in cur.description]
            rows = [dict(zip(cols, r)) for r in await cur.fetchall()]
            for r in rows:
                r["record_id"] = int(r["record_id"])
            return rows


async def records_between(
    session_id: int,
    after_id: int,
    until_id: Optional[int] = None,
    limit: int = 40,
) -> list[dict]:
    """取 (after_id, until_id] 区间内按 record_id 正序的记录；供滚动摘要与事实抽取。"""
    pool = get_pool()
    params: dict = {"sid": int(session_id), "after": int(after_id or 0), "n": max(1, min(int(limit), 100))}
    upper_sql = ""
    if until_id is not None:
        upper_sql = "AND record_id <= %(until)s"
        params["until"] = int(until_id)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT record_id, query, answer, answer_digest
                FROM chat_record
                WHERE session_id = %(sid)s
                  AND record_id > %(after)s
                  {upper_sql}
                ORDER BY record_id ASC
                LIMIT %(n)s
                """,
                params,
            )
            return [
                {"record_id": int(r[0]), "query": r[1], "answer": r[2], "answer_digest": r[3]}
                for r in await cur.fetchall()
            ]


async def count_records_after(session_id: int, after_id: int) -> int:
    """统计水位之后还有多少轮未摘要记录。"""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM chat_record WHERE session_id = %(sid)s AND record_id > %(after)s",
                {"sid": int(session_id), "after": int(after_id or 0)},
            )
            return int((await cur.fetchone())[0])


async def update_summary(session_id: int, user_id: int, summary: str, until_record_id: int) -> bool:
    """条件推进摘要水位：仅当新水位严格大于旧水位时写入，避免并发重复摘要覆盖。"""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE chat_session
                    SET summary = %(s)s,
                        summarized_until_record_id = %(until)s
                    WHERE session_id = %(id)s AND user_id = %(uid)s
                      AND (summarized_until_record_id IS NULL
                           OR summarized_until_record_id < %(until)s)
                    RETURNING session_id
                    """,
                    {
                        "s": (summary or "")[:2000],
                        "until": int(until_record_id),
                        "id": int(session_id),
                        "uid": int(user_id),
                    },
                )
                return (await cur.fetchone()) is not None


async def set_memory_enabled(session_id: int, user_id: int, enabled: bool) -> Optional[dict]:
    """切换会话记忆开关（临时会话）；越权/不存在返回 None。"""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE chat_session
                    SET memory_enabled = %(enabled)s
                    WHERE session_id = %(id)s AND user_id = %(uid)s
                    RETURNING session_id, session_name, summary, summarized_until_record_id, memory_enabled
                    """,
                    {"enabled": bool(enabled), "id": int(session_id), "uid": int(user_id)},
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                return {
                    "session_id": int(row[0]),
                    "session_name": row[1],
                    "summary": row[2] or "",
                    "summarized_until_record_id": int(row[3]) if row[3] is not None else 0,
                    "memory_enabled": bool(row[4]),
                }


async def reset_memory(session_id: int, user_id: int) -> bool:
    """清空会话记忆：摘要置空，水位推到最新记录（旧对话不再重新摘要）。

    事实清理由 session_memory_store.clear_facts 负责；本函数只管 chat_session 侧。
    """
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT COALESCE(MAX(record_id), 0) FROM chat_record WHERE session_id = %(sid)s",
                    {"sid": int(session_id)},
                )
                latest = int((await cur.fetchone())[0])
                await cur.execute(
                    """
                    UPDATE chat_session
                    SET summary = '',
                        summarized_until_record_id = %(until)s
                    WHERE session_id = %(id)s AND user_id = %(uid)s
                    RETURNING session_id
                    """,
                    {"until": latest, "id": int(session_id), "uid": int(user_id)},
                )
                return (await cur.fetchone()) is not None
