"""每用户配额与限流（v3 / WP5-6）。

设计要点：
1. **日 token 配额**：按 `chat_io_log` 当日 `total_tokens` 汇总（每轮 LLM 用量已按 trace 聚合，
   且带 user_id；`llm_call_log.user_id` 在线程调用里可能为空，不能作为用户级口径），
   达到上限后当日拒绝新问答；配额读的是"已经消耗掉"的真实用量，不做预估。
2. **每分钟请求数**：用 `chat_rate_window` 表按自然分钟做原子 UPSERT 自增，
   跨进程/重启都有效；被拒绝的请求也会计数，避免"被限流后继续重试"绕过。
3. **失败不阻断主链路**：限流表不可用时放行并告警一次，避免把数据库抖动放大成"全站不能用"。
4. 配置来自 `app_config`（可在配置中心在线调整），关闭 `QUOTA_ENABLED` 后完全跳过。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from ..db import get_pool
from ..errors import ERR_QUOTA_EXCEEDED, ERR_RATE_LIMITED
from . import app_config

logger = logging.getLogger("rag.quota")
_warned = False
_last_prune = 0.0


async def ensure_schema() -> None:
    """启动时幂等建表；数据库不可用只告警，不阻断启动。"""
    try:
        pool = get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_rate_window (
                    user_id      BIGINT      NOT NULL,
                    window_start TIMESTAMP   NOT NULL,
                    request_count INTEGER    NOT NULL DEFAULT 0,
                    updated_at   TIMESTAMP   NOT NULL DEFAULT now(),
                    PRIMARY KEY (user_id, window_start)
                )
                """
            )
    except Exception:  # noqa: BLE001 - 建表失败不阻断启动，首次请求会再试
        logger.warning("配额限流表初始化失败，将在首次请求时重试", exc_info=True)


async def _daily_tokens(user_id: int) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT COALESCE(SUM(COALESCE(total_tokens, 0)), 0) AS used
            FROM chat_io_log
            WHERE user_id = %(user_id)s
              AND created_at >= date_trunc('day', now())
            """,
            {"user_id": user_id},
        )
        row = await cur.fetchone()
        return int((row or (0,))[0] or 0)


async def _bump_rate(user_id: int) -> int:
    """把当前自然分钟的计数 +1 并返回计数；表不存在时先建一次再重试。"""
    global _last_prune
    pool = get_pool()
    async with pool.connection() as conn:
        try:
            cur = await conn.execute(
                """
                INSERT INTO chat_rate_window (user_id, window_start, request_count)
                VALUES (%(user_id)s, date_trunc('minute', now()), 1)
                ON CONFLICT (user_id, window_start)
                DO UPDATE SET request_count = chat_rate_window.request_count + 1,
                              updated_at = now()
                RETURNING request_count
                """,
                {"user_id": user_id},
            )
        except Exception:
            # 首次请求时表可能还没建好；回滚失败事务后建表重试一次，仍失败则交给上层放行。
            await conn.rollback()
            await ensure_schema()
            cur = await conn.execute(
                """
                INSERT INTO chat_rate_window (user_id, window_start, request_count)
                VALUES (%(user_id)s, date_trunc('minute', now()), 1)
                ON CONFLICT (user_id, window_start)
                DO UPDATE SET request_count = chat_rate_window.request_count + 1,
                              updated_at = now()
                RETURNING request_count
                """,
                {"user_id": user_id},
            )
        row = await cur.fetchone()
        count_value = int((row or (0,))[0] or 0)
        # 低频清理历史窗口，避免表无限增长；每分钟最多一次。
        now = time.monotonic()
        if now - _last_prune > 60:
            _last_prune = now
            await conn.execute(
                "DELETE FROM chat_rate_window WHERE window_start < date_trunc('minute', now()) - interval '2 days'"
            )
        return count_value


async def check_and_consume(user_id: int) -> dict[str, Any]:
    """在建立 SSE 流之前调用；返回 {allowed, code, msg, ...}。

    - 超限时返回 `allowed=False`，由路由层落 rejected 日志并抛 BizError；
    - 数据库异常时放行（可用性优先），只告警一次。
    """
    global _warned
    enabled = bool(await app_config.get_value("QUOTA_ENABLED"))
    if not enabled:
        return {"allowed": True, "enabled": False}

    rate_limit = int(await app_config.get_value("QUOTA_RATE_LIMIT_PER_MINUTE"))
    token_limit = int(await app_config.get_value("QUOTA_DAILY_TOKEN_LIMIT"))
    try:
        count = await _bump_rate(user_id)
        if count > rate_limit:
            return {
                "allowed": False,
                "code": ERR_RATE_LIMITED,
                "reason": "rate",
                "msg": f"请求过于频繁（每分钟最多 {rate_limit} 次），请稍后再试",
                "limit": rate_limit,
                "current": count,
            }
        used = await _daily_tokens(user_id)
        if used >= token_limit:
            return {
                "allowed": False,
                "code": ERR_QUOTA_EXCEEDED,
                "reason": "daily",
                "msg": f"今日 token 配额已用完（{used}/{token_limit}），请明日再试或联系管理员调整上限",
                "limit": token_limit,
                "used": used,
            }
    except Exception:  # noqa: BLE001 - 配额服务故障不能阻断问答
        if not _warned:
            _warned = True
            logger.warning("配额/限流检查失败，已放行本次请求", exc_info=True)
        return {"allowed": True, "degraded": True}

    return {"allowed": True, "enabled": True, "rate_current": count, "used": used}


def reset_warning_state() -> None:
    """测试辅助：重置只告警一次的标记。"""
    global _warned
    _warned = False
