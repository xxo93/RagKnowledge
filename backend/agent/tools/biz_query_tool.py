"""业务库查询工具（M4）。

职责边界：
- 只做"把用户问题交给 biz_query 服务"的桥接；权限、参数、SQL 合规、脱敏、行数限制全部在
  `services.biz_query` 内完成，工具本身不拼接任何 SQL；
- 失败一律返回结构化原因（可供 SSE token 直接展示），不抛异常击穿状态图。
"""
from __future__ import annotations

import logging

from ...services import biz_query

logger = logging.getLogger("rag.tool.biz")


async def run(query: str, user: dict | None = None) -> dict:
    user = user or {"user_id": 0, "role": "member"}
    try:
        return await biz_query.decide_and_execute(query, user)
    except Exception as exc:  # noqa: BLE001 - 工具失败不击穿图
        logger.exception("biz_query 工具执行异常")
        return {"ok": False, "code": "BIZ_EXEC_ERROR", "message": "业务库查询失败，请稍后重试"}
