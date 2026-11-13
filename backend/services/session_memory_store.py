"""会话事实存储（session_memory 表）。

隔离规则（本版最高优先级回归项）：
- 所有 SQL 都带 `session_id = ?`，读写均校验 `user_id` 归属；
- 写操作先确认会话属于当前用户，越权返回 None，绝不触碰他人会话；
- 同一会话同一 (kind, fact_key) 只允许一条 active：新事实先作废旧版本再插入。

本模块只做 SQL，抽取/归一化在 services/session_fact.py。
"""
from __future__ import annotations

import logging
from typing import Optional

import psycopg

from ..config import settings
from ..db import get_pool

logger = logging.getLogger("rag.session_memory_store")

KINDS = ("constraint", "fact", "entity", "todo")


def detect_change(previous: Optional[str], new_value: str) -> tuple[Optional[str], bool]:
    """返回 (需要展示的旧值, 是否真的变更)；同值覆盖不算口径变更。"""
    new = (new_value or "").strip()[:500]
    if previous is None or previous == new:
        return None, False
    return previous, True

_DDL = (
    """
    CREATE TABLE IF NOT EXISTS session_memory (
      mem_id          BIGSERIAL PRIMARY KEY,
      session_id      BIGINT NOT NULL REFERENCES chat_session(session_id) ON DELETE CASCADE,
      user_id         BIGINT NOT NULL REFERENCES app_user(user_id) ON DELETE CASCADE,
      kind            VARCHAR(16) NOT NULL,
      fact_key        VARCHAR(64) NOT NULL,
      fact_value      TEXT NOT NULL,
      confidence      DOUBLE PRECISION NOT NULL DEFAULT 1.0,
      source_record_id BIGINT NULL,
      invalidated_at  TIMESTAMP NULL DEFAULT NULL,
      create_time     TIMESTAMP NOT NULL DEFAULT now(),
      update_time     TIMESTAMP NOT NULL DEFAULT now(),
      CONSTRAINT ck_session_memory_kind CHECK (kind IN ('constraint', 'fact', 'entity', 'todo'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_session_memory_active
      ON session_memory (session_id, kind, update_time DESC) WHERE invalidated_at IS NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_session_memory_active_key
      ON session_memory (session_id, kind, fact_key) WHERE invalidated_at IS NULL
    """,
    # v3 / M8 WP5-8：同 key 被改写时保留上一版值，面板据此提示「口径已变更」
    "ALTER TABLE session_memory ADD COLUMN IF NOT EXISTS previous_value TEXT DEFAULT NULL",
    "ALTER TABLE session_memory ADD COLUMN IF NOT EXISTS value_changed_at TIMESTAMP DEFAULT NULL",
    # v4 / N5：事实每次真正改值追加一条历史，面板可看多版口径演变
    """
    CREATE TABLE IF NOT EXISTS session_memory_history (
      history_id       BIGSERIAL PRIMARY KEY,
      session_id       BIGINT NOT NULL REFERENCES chat_session(session_id) ON DELETE CASCADE,
      fact_id          BIGINT NOT NULL REFERENCES session_memory(mem_id) ON DELETE CASCADE,
      fact_key         VARCHAR(64) NOT NULL,
      old_value        TEXT NOT NULL,
      new_value        TEXT NOT NULL,
      changed_at       TIMESTAMP NOT NULL DEFAULT now(),
      source_record_id BIGINT NULL,
      created_at       TIMESTAMP NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_session_memory_history_fact
      ON session_memory_history (fact_id, changed_at DESC, history_id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_session_memory_history_session
      ON session_memory_history (session_id, changed_at DESC)
    """,
)


async def ensure_memory_schema() -> None:
    """启动时建表/补索引（幂等）；失败只告警，不阻断启动。"""
    try:
        pool = get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                for sql in _DDL:
                    await conn.execute(sql)
    except Exception as exc:  # noqa: BLE001
        logger.warning("session_memory 建表失败：%s", type(exc).__name__)


async def _owns_session(cur, session_id: int, user_id: int) -> bool:
    """会话归属校验：所有写操作的前置条件。"""
    await cur.execute(
        "SELECT 1 FROM chat_session WHERE session_id = %(id)s AND user_id = %(uid)s",
        {"id": int(session_id), "uid": int(user_id)},
    )
    return (await cur.fetchone()) is not None


async def list_facts(session_id: int, user_id: int, limit: int = 20) -> list[dict]:
    """列出当前 active 事实；越权会话返回空列表（路由层会先做 40402 判定）。"""
    pool = get_pool()
    n = max(1, min(int(limit or 20), 50))
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT m.mem_id, m.kind, m.fact_key, m.fact_value, m.confidence,
                       m.source_record_id, m.create_time, m.update_time,
                       m.previous_value, m.value_changed_at
                FROM session_memory m
                JOIN chat_session s
                  ON s.session_id = m.session_id AND s.user_id = %(uid)s
                WHERE m.session_id = %(id)s
                  AND m.invalidated_at IS NULL
                ORDER BY m.kind ASC, m.update_time DESC
                LIMIT %(n)s
                """,
                {"id": int(session_id), "uid": int(user_id), "n": n},
            )
            cols = [c.name for c in cur.description]
            rows = []
            for r in await cur.fetchall():
                item = dict(zip(cols, r))
                item["mem_id"] = int(item["mem_id"])
                item["confidence"] = float(item["confidence"])
                if item.get("source_record_id") is not None:
                    item["source_record_id"] = int(item["source_record_id"])
                rows.append(item)
            return rows


async def upsert_fact(
    session_id: int,
    user_id: int,
    kind: str,
    fact_key: str,
    fact_value: str,
    confidence: float = 1.0,
    source_record_id: Optional[int] = None,
) -> Optional[dict]:
    """写入一条事实：同 key 复用同一 active 行；真正改值时同事务追加历史。

    首写只插入 active 行（不写历史）；同值覆盖不写历史；返回行；
    会话不属于该用户或不存在返回 None。
    """
    if kind not in KINDS:
        raise ValueError(f"非法事实类型：{kind}")
    key = (fact_key or "").strip()[:64]
    if not key:
        raise ValueError("事实 key 不能为空")
    value = (fact_value or "").strip()
    if not value:
        raise ValueError("事实内容不能为空")

    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                if not await _owns_session(cur, session_id, user_id):
                    return None
                await cur.execute(
                    """
                    SELECT mem_id, fact_value
                    FROM session_memory
                    WHERE session_id = %(sid)s AND kind = %(kind)s
                      AND fact_key = %(key)s AND invalidated_at IS NULL
                    LIMIT 1
                    """,
                    {"sid": int(session_id), "kind": kind, "key": key},
                )
                old = await cur.fetchone()
                new_value = value[:500]
                if old is None:
                    # 首写：只建 active 行，不写历史
                    await cur.execute(
                        """
                        INSERT INTO session_memory
                            (session_id, user_id, kind, fact_key, fact_value, confidence,
                             source_record_id, previous_value, value_changed_at)
                        VALUES (%(sid)s, %(uid)s, %(kind)s, %(key)s, %(val)s, %(conf)s,
                                %(src)s, NULL, NULL)
                        RETURNING mem_id, kind, fact_key, fact_value, confidence, source_record_id,
                                  update_time, previous_value, value_changed_at
                        """,
                        {
                            "sid": int(session_id),
                            "uid": int(user_id),
                            "kind": kind,
                            "key": key,
                            "val": new_value,
                            "conf": max(0.0, min(float(confidence), 1.0)),
                            "src": int(source_record_id) if source_record_id is not None else None,
                        },
                    )
                    row = await cur.fetchone()
                else:
                    mem_id, previous = int(old[0]), old[1]
                    prev_shown, changed = detect_change(previous, new_value)
                    # 复用同一 mem_id：历史接口按稳定 fact_id 可查完整版本链
                    await cur.execute(
                        """
                        UPDATE session_memory
                        SET fact_value = %(val)s,
                            confidence = %(conf)s,
                            source_record_id = %(src)s,
                            previous_value = CASE WHEN %(changed)s THEN %(prev)s ELSE previous_value END,
                            value_changed_at = CASE WHEN %(changed)s THEN now() ELSE value_changed_at END,
                            update_time = now()
                        WHERE mem_id = %(mid)s AND session_id = %(sid)s
                          AND invalidated_at IS NULL
                        RETURNING mem_id, kind, fact_key, fact_value, confidence, source_record_id,
                                  update_time, previous_value, value_changed_at
                        """,
                        {
                            "mid": mem_id,
                            "sid": int(session_id),
                            "val": new_value,
                            "conf": max(0.0, min(float(confidence), 1.0)),
                            "src": int(source_record_id) if source_record_id is not None else None,
                            "changed": changed,
                            "prev": prev_shown,
                        },
                    )
                    row = await cur.fetchone()
                    if changed:
                        await cur.execute(
                            """
                            INSERT INTO session_memory_history
                                (session_id, fact_id, fact_key, old_value, new_value,
                                 changed_at, source_record_id)
                            VALUES (%(sid)s, %(mid)s, %(key)s, %(old)s, %(new)s, now(), %(src)s)
                            """,
                            {
                                "sid": int(session_id),
                                "mid": mem_id,
                                "key": key,
                                "old": previous,
                                "new": new_value,
                                "src": int(source_record_id) if source_record_id is not None else None,
                            },
                        )
                        limit_n = max(1, min(int(settings.SESSION_FACT_HISTORY_LIMIT), 100))
                        await cur.execute(
                            """
                            WITH ranked AS (
                              SELECT history_id,
                                     row_number() OVER (
                                       PARTITION BY fact_id ORDER BY changed_at DESC, history_id DESC
                                     ) AS rn
                              FROM session_memory_history
                              WHERE fact_id = %(mid)s
                            )
                            DELETE FROM session_memory_history
                            WHERE history_id IN (SELECT history_id FROM ranked WHERE rn > %(n)s)
                            """,
                            {"mid": mem_id, "n": limit_n},
                        )
                return {
                    "mem_id": int(row[0]),
                    "kind": row[1],
                    "fact_key": row[2],
                    "fact_value": row[3],
                    "confidence": float(row[4]),
                    "source_record_id": int(row[5]) if row[5] is not None else None,
                    "update_time": row[6],
                    "previous_value": row[7],
                    "value_changed_at": row[8],
                }


async def invalidate_fact(session_id: int, user_id: int, mem_id: int) -> bool:
    """删除（软失效）单条事实；不属于该用户或已失效返回 False。"""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                if not await _owns_session(cur, session_id, user_id):
                    return False
                await cur.execute(
                    """
                    UPDATE session_memory
                    SET invalidated_at = now()
                    WHERE mem_id = %(mid)s AND session_id = %(sid)s
                      AND invalidated_at IS NULL
                    RETURNING mem_id
                    """,
                    {"mid": int(mem_id), "sid": int(session_id)},
                )
                if (await cur.fetchone()) is None:
                    return False
                # N5：删除事实时同步删历史，避免孤儿版本
                await cur.execute(
                    "DELETE FROM session_memory_history WHERE session_id = %(sid)s AND fact_id = %(mid)s",
                    {"mid": int(mem_id), "sid": int(session_id)},
                )
                return True


async def clear_facts(session_id: int, user_id: int) -> int:
    """清空会话事实（保留表结构）；返回失效条数。"""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                if not await _owns_session(cur, session_id, user_id):
                    return 0
                await cur.execute(
                    """
                    UPDATE session_memory
                    SET invalidated_at = now()
                    WHERE session_id = %(sid)s AND invalidated_at IS NULL
                    """,
                    {"sid": int(session_id)},
                )
                removed = cur.rowcount or 0
                await cur.execute(
                    "DELETE FROM session_memory_history WHERE session_id = %(sid)s",
                    {"sid": int(session_id)},
                )
                return removed


async def list_fact_history(session_id: int, user_id: int, mem_id: int) -> Optional[list[dict]]:
    """N5：某条事实的完整改值时间线；事实不存在/越权返回 None（路由层转 40402）。"""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT 1
                FROM session_memory m
                JOIN chat_session s ON s.session_id = m.session_id AND s.user_id = %(uid)s
                WHERE m.session_id = %(sid)s AND m.mem_id = %(mid)s AND m.invalidated_at IS NULL
                """,
                {"sid": int(session_id), "uid": int(user_id), "mid": int(mem_id)},
            )
            if (await cur.fetchone()) is None:
                return None
            await cur.execute(
                """
                SELECT history_id, session_id, fact_id, fact_key, old_value, new_value,
                       changed_at, source_record_id, created_at
                FROM session_memory_history
                WHERE session_id = %(sid)s AND fact_id = %(mid)s
                ORDER BY changed_at DESC, history_id DESC
                """,
                {"sid": int(session_id), "mid": int(mem_id)},
            )
            cols = [c.name for c in cur.description]
            rows = []
            for r in await cur.fetchall():
                item = dict(zip(cols, r))
                item["history_id"] = int(item["history_id"])
                item["session_id"] = int(item["session_id"])
                item["fact_id"] = int(item["fact_id"])
                if item.get("source_record_id") is not None:
                    item["source_record_id"] = int(item["source_record_id"])
                rows.append(item)
            return rows


async def count_facts(session_id: int) -> int:
    """统计 active 事实条数（内部观测用，不做用户归属判定）。"""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM session_memory WHERE session_id = %(sid)s AND invalidated_at IS NULL",
                {"sid": int(session_id)},
            )
            return int((await cur.fetchone())[0])
