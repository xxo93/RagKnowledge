"""管理类与权限类操作审计。

只记录管理类与权限类操作，**不记录聊天问答内容**。
审计写入失败绝不影响主流程：统一降级为 warning 日志。
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from db import get_pool

logger = logging.getLogger("rag.audit")

# 审计动作常量
AUTH_LOGIN = "AUTH_LOGIN"
AUTH_PASSWORD = "AUTH_PASSWORD"
DOC_UPLOAD = "DOC_UPLOAD"
DOC_DELETE = "DOC_DELETE"
REPARSE = "REPARSE"
REINDEX = "REINDEX"
USER_MANAGE = "USER_MANAGE"


async def log_operation(
    actor_user_id: Optional[int],
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    result: int = 1,
    detail: Optional[dict] = None,
) -> None:
    """写一条审计记录；任何异常都被吞掉并记 warning，不阻断业务。"""
    try:
        pool = get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO operation_log
                            (actor_user_id, action, target_type, target_id, result, detail)
                        VALUES (%(actor)s, %(action)s, %(ttype)s, %(tid)s, %(result)s, %(detail)s::jsonb)
                        """,
                        {
                            "actor": actor_user_id,
                            "action": action,
                            "ttype": target_type,
                            "tid": str(target_id) if target_id is not None else None,
                            "result": int(result),
                            "detail": json.dumps(detail, ensure_ascii=False, default=str) if detail else None,
                        },
                    )
    except Exception:  # noqa: BLE001 - 审计失败不得影响业务
        logger.warning("审计写入失败 action=%s actor=%s（不影响主流程）", action, actor_user_id, exc_info=True)
