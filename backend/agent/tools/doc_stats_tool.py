"""文档统计工具。

安全要点：stat_type 必须命中枚举白名单；SQL 由 vector_store 内的常量映射提供，
本工具与调用链均不拼接用户字符串。
"""
from __future__ import annotations

import logging

from ...services import vector_store as vs

logger = logging.getLogger("rag.tool.stats")

# 枚举白名单（与 vector_store._STAT_SQL 键保持一致）
ALLOWED_STATS = {"total_docs", "success_docs", "failed_docs", "total_chunks"}


async def run(stat_type: str) -> dict:
    stat_type = (stat_type or "").strip()
    if stat_type not in ALLOWED_STATS:
        return {
            "ok": False,
            "stat_type": stat_type,
            "value": None,
            "error": f"不支持的统计类型，可选：{', '.join(sorted(ALLOWED_STATS))}",
        }
    try:
        value = await vs.count_stat(stat_type)
        return {"ok": True, "stat_type": stat_type, "value": value, "error": None}
    except Exception as exc:  # noqa: BLE001
        logger.exception("doc_stats 执行异常")
        return {"ok": False, "stat_type": stat_type, "value": None, "error": str(exc)}
