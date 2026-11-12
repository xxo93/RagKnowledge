"""知识库检索工具。

入参：{"query": str}；出参：{"ok", "answer", "sources", "error"}。
工具内部不直接读写数据库，统一经 services.rag。
工具异常被捕获为 ok=False，由上层回传 LLM 组织回复。
"""
from __future__ import annotations

import logging

from ...services import rag

logger = logging.getLogger("rag.tool.search")

MAX_QUERY_LEN = 500


async def run(query: str) -> dict:
    query = (query or "").strip()
    if not query:
        return {"ok": False, "answer": "", "sources": [], "error": "检索问题为空"}
    if len(query) > MAX_QUERY_LEN:
        query = query[:MAX_QUERY_LEN]
    try:
        result = await rag.answer(query)
        return {
            "ok": True,
            "answer": result.answer,
            "sources": result.sources,
            "hits": result.hits,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - 工具异常回传，不击穿状态图
        logger.exception("knowledge_search 执行异常")
        return {"ok": False, "answer": "", "sources": [], "error": str(exc)}
