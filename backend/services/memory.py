"""会话上下文记忆（F-11 / 架构 §3.4）。

- 取最近 HISTORY_ROUNDS 轮（一问一答 = 1 条 chat_record，含 user+assistant 两条消息）；
- 单条答案超长截断（默认 800 字），控制 token 与费用；
- SQL 集中在 services/chat_store；本模块只做查询装配、角色映射与截断规则；
- 记忆读取失败不阻断主问答，降级返回空历史（避免历史故障击穿主链路）。
"""
from __future__ import annotations

import logging

from config import settings
from services import chat_store

logger = logging.getLogger("rag.memory")

MAX_ANSWER_CHARS = 800


async def load_recent(session_id: int | None, rounds: int | None = None) -> list[dict]:
    """返回按时间正序的 [{"role": "user"/"assistant", "content": str}]，供 build_prompt 使用。

    - session_id 为空（无会话）时返回 []；
    - 仓储异常降级返回 []，不抛到 SSE 主链路；
    - recent_records 按 record_id 倒序取出，此处反转为正序。
    """
    if not session_id:
        return []
    rounds = rounds or settings.HISTORY_ROUNDS
    try:
        rows = await chat_store.recent_records(int(session_id), rounds)
    except Exception:  # noqa: BLE001 - 记忆降级，不击穿问答
        logger.exception("读取最近会话历史失败，按空历史继续")
        return []

    messages: list[dict] = []
    for row in reversed(rows):
        query = (row.get("query") or "").strip()
        answer = (row.get("answer") or "").strip()
        if query:
            messages.append({"role": "user", "content": query})
        if answer:
            messages.append({"role": "assistant", "content": answer})
    return trim_messages(messages)


def trim_messages(messages: list[dict]) -> list[dict]:
    """对 assistant 内容做长度截断，避免上下文膨胀。"""
    out: list[dict] = []
    for m in messages:
        content = m.get("content", "")
        if m.get("role") == "assistant" and len(content) > MAX_ANSWER_CHARS:
            content = content[:MAX_ANSWER_CHARS] + "……"
        out.append({**m, "content": content})
    return out
