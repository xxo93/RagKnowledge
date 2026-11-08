"""会话上下文记忆（F-11 / 架构 §3.4）。

- 取最近 HISTORY_ROUNDS 轮（一问一答 = 2 条）作为 Agent 上下文；
- 单条答案超长截断（默认 800 字），控制 token 与费用；
- SQL 仍集中在仓储层，本模块只做查询装配与截断规则。
"""
from __future__ import annotations

from config import settings

MAX_ANSWER_CHARS = 800


async def load_recent(session_id: int, rounds: int | None = None) -> list[dict]:
    """返回 [{"role": "user"/"assistant", "content": str}]，按时间正序，供 Prompt 使用。

    TODO(M4): 经仓储查询最近 N 条记录并转换为 messages；当前为契约占位。
    """
    rounds = rounds or settings.HISTORY_ROUNDS
    # 仓储 SQL 应形如：
    # SELECT query, answer FROM chat_record
    # WHERE session_id = %(sid)s ORDER BY record_id DESC LIMIT %(n)s;
    # 随后反转、截断超长 answer。
    return []


def trim_messages(messages: list[dict]) -> list[dict]:
    """对 assistant 内容做长度截断，避免上下文膨胀。"""
    out: list[dict] = []
    for m in messages:
        content = m.get("content", "")
        if m.get("role") == "assistant" and len(content) > MAX_ANSWER_CHARS:
            content = content[:MAX_ANSWER_CHARS] + "……"
        out.append({**m, "content": content})
    return out
