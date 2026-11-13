"""会话上下文记忆装配（v3 / WP1）。

固定注入顺序（需求 §3.1）：
    系统提示 → 会话事实 → 滚动摘要 → 最近 N 轮原文 → 检索资料 → 本轮问题

预算治理（需求 §3.2）：
- 双闸门：轮数上限 HISTORY_WINDOW_ROUNDS + token 预算 HISTORY_MAX_TOKENS；
- 超预算时先砍最旧的原文轮次，**会话事实与摘要始终保留**（它们信息密度最高）；
- 每轮答案用 answer_digest（头 300 字 + 尾 150 字）替代全文，单轮再做 token 上限兜底。

其它硬约束：
- 所有查询按 session_id 隔离，并校验 user_id 归属（需求 §3.5）；
- memory_enabled=false（临时会话）时，事实与摘要都不注入，也不做自动抽取；
- 读取失败降级为空上下文，绝不击穿主问答链路。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from ..config import settings
from . import app_config, chat_store, session_memory_store

logger = logging.getLogger("rag.memory")

# 兼容旧配置的答案硬截断（仅在既无 digest 又无法生成时兜底）
MAX_ANSWER_CHARS = 800

KIND_LABELS = {
    "constraint": "约束",
    "fact": "事实",
    "entity": "实体",
    "todo": "待办",
}

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


# ---------------------------------------------------------------- 工具函数
def estimate_tokens(text: str) -> int:
    """无 tokenizer 依赖的保守估算：CJK 字符按 1 token，其它按 4 字符 1 token。

    只用于窗口预算的**相对**裁剪与观测展示，不用于计费；真实用量以模型返回 usage 为准。
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = max(0, len(text) - cjk)
    return int(cjk + other / 4.0) + (1 if text else 0)


def make_digest(answer: str, head: int | None = None, tail: int | None = None) -> str:
    """生成 answer_digest：头 300 字 + 省略标记 + 尾 150 字；短答案原样返回。"""
    text = (answer or "").strip()
    if not text:
        return ""
    head = int(settings.HISTORY_ANSWER_HEAD_CHARS if head is None else head)
    tail = int(settings.HISTORY_ANSWER_TAIL_CHARS if tail is None else tail)
    if len(text) <= head + tail + 10:
        return text
    return f"{text[:head]}……{text[-tail:]}"


def _clip_to_tokens(text: str, max_tokens: int) -> str:
    """按估算 token 裁剪文本（保留开头），用于单轮兜底。"""
    text = text or ""
    if max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text
    # 中文为主时按 1:1 估，直接按字符裁；留 5% 余量避免估算误差
    keep = max(1, int(max_tokens * 0.95))
    return text[:keep] + "……"


def format_facts(facts: list[dict]) -> str:
    """把事实列表渲染成注入块；同类合并，保持稳定顺序。"""
    if not facts:
        return ""
    lines = []
    for item in facts:
        kind = item.get("kind") or "fact"
        label = KIND_LABELS.get(kind, "事实")
        key = (item.get("fact_key") or "").strip()
        value = (item.get("fact_value") or "").strip()
        if not value:
            continue
        lines.append(f"- [{label}] {key}：{value}" if key else f"- [{label}] {value}")
    if not lines:
        return ""
    return "【本次会话已确认的信息】\n" + "\n".join(lines)


def format_summary(summary: str) -> str:
    """滚动摘要注入块。"""
    text = (summary or "").strip()
    if not text:
        return ""
    return "【更早对话摘要】\n" + text


# ---------------------------------------------------------------- 上下文装配
@dataclass
class ContextBundle:
    """装配结果：可直接注入 prompt 的块 + 观测用量。"""

    history: list[dict] = field(default_factory=list)
    facts_block: str = ""
    summary_block: str = ""
    facts: list[dict] = field(default_factory=list)
    summary: str = ""
    memory_enabled: bool = True
    usage: dict = field(default_factory=dict)

    def extra_system(self) -> list[str]:
        """事实与摘要作为独立 system 块，插在系统提示之后、检索资料之前。"""
        blocks = [b for b in (self.facts_block, self.summary_block) if b]
        return blocks


async def build_context(session_id: Optional[int], user_id: int) -> ContextBundle:
    """装配会话上下文（事实 + 摘要 + 最近 N 轮原文），并返回 token 用量观测。"""
    if not session_id:
        return ContextBundle(usage=_usage_empty())

    try:
        state = await chat_store.get_memory_state(int(session_id), int(user_id))
    except Exception:  # noqa: BLE001 - 记忆降级，不击穿问答
        logger.exception("读取会话记忆状态失败，按空上下文继续")
        return ContextBundle(usage=_usage_empty())

    if state is None:
        return ContextBundle(usage=_usage_empty())

    enabled = bool(state.get("memory_enabled", True))
    facts: list[dict] = []
    summary = ""

    if enabled:
        try:
            facts = await session_memory_store.list_facts(
                int(session_id), int(user_id), limit=int(settings.SESSION_FACT_MAX_ITEMS)
            )
        except Exception:  # noqa: BLE001
            logger.warning("读取会话事实失败，降级为无事实", exc_info=True)
            facts = []
        summary = (state.get("summary") or "").strip()

    facts_block = format_facts(facts)
    summary_block = format_summary(summary)
    # 运行时配置中心可在线调整（保存后缓存立即刷新，下一条请求生效）
    try:
        budget = int(await app_config.get_value("HISTORY_MAX_TOKENS"))
        window_rounds = max(1, int(await app_config.get_value("HISTORY_WINDOW_ROUNDS")))
        turn_cap = max(60, int(await app_config.get_value("HISTORY_TURN_MAX_TOKENS")))
    except Exception:  # noqa: BLE001 - 配置读取失败回退 .env/代码默认
        budget = int(settings.HISTORY_MAX_TOKENS)
        window_rounds = max(1, int(settings.HISTORY_WINDOW_ROUNDS))
        turn_cap = max(60, int(settings.HISTORY_TURN_MAX_TOKENS))
    fact_tokens = estimate_tokens(facts_block)
    summary_tokens = estimate_tokens(summary_block)

    # 事实/摘要优先占用预算；剩余才给原文轮次（需求 §3.2）
    remaining = budget - fact_tokens - summary_tokens
    history: list[dict] = []
    dropped_rounds = 0
    used_tokens = 0
    rounds_used = 0
    if remaining > 0:
        try:
            # 多取一倍，确保在预算裁剪后仍能填满窗口
            rows = await chat_store.context_records(int(session_id), window_rounds * 2)
        except Exception:  # noqa: BLE001
            logger.warning("读取最近会话历史失败，按空历史继续", exc_info=True)
            rows = []

        for row in rows:  # 已按 record_id 倒序
            if rounds_used >= window_rounds:
                dropped_rounds += 1
                continue
            query = (row.get("query") or "").strip()
            if not query:
                continue
            answer_digest = (row.get("answer_digest") or "").strip() or make_digest(
                row.get("answer") or ""
            )
            answer_digest = _clip_to_tokens(answer_digest, turn_cap)
            turn_tokens = estimate_tokens(query) + estimate_tokens(answer_digest)
            if turn_tokens > remaining - used_tokens:
                # 预算不够，停止继续向前取更旧轮次（更新的已保留）
                dropped_rounds += 1
                break
            pair: list[dict] = [{"role": "user", "content": query}]
            if answer_digest:
                pair.append({"role": "assistant", "content": answer_digest})
            history = pair + history
            used_tokens += turn_tokens
            rounds_used += 1

    usage = {
        "memory_enabled": enabled,
        "budget_tokens": budget,
        "history_rounds": rounds_used,
        "history_tokens": used_tokens,
        "facts_count": len(facts),
        "facts_tokens": fact_tokens,
        "summary_chars": len(summary),
        "summary_tokens": summary_tokens,
        "total_tokens": fact_tokens + summary_tokens + used_tokens,
        "dropped_rounds": dropped_rounds,
        "window_rounds": window_rounds,
        "token_estimator": "cjk=1/char, other=1/4char",
    }
    return ContextBundle(
        history=history,
        facts_block=facts_block,
        summary_block=summary_block,
        facts=facts,
        summary=summary,
        memory_enabled=enabled,
        usage=usage,
    )


def _usage_empty() -> dict:
    return {
        "memory_enabled": False,
        "budget_tokens": int(settings.HISTORY_MAX_TOKENS),
        "history_rounds": 0,
        "history_tokens": 0,
        "facts_count": 0,
        "facts_tokens": 0,
        "summary_chars": 0,
        "summary_tokens": 0,
        "total_tokens": 0,
        "dropped_rounds": 0,
        "window_rounds": int(settings.HISTORY_WINDOW_ROUNDS),
        "token_estimator": "cjk=1/char, other=1/4char",
    }


# ---------------------------------------------------------------- 兼容旧入口
async def load_recent(session_id: int | None, rounds: int | None = None) -> list[dict]:
    """旧调用入口：只返回最近轮次原文（不做事实/摘要装配）。

    新代码请用 build_context(session_id, user_id)；本函数保留给旧测试与工具层。
    """
    if not session_id:
        return []
    rounds = rounds or int(settings.HISTORY_ROUNDS)
    try:
        rows = await chat_store.recent_records(int(session_id), rounds)
    except Exception:  # noqa: BLE001
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
    """旧逻辑：对 assistant 内容做长度截断。"""
    out: list[dict] = []
    for m in messages:
        content = m.get("content", "")
        if m.get("role") == "assistant" and len(content) > MAX_ANSWER_CHARS:
            content = content[:MAX_ANSWER_CHARS] + "……"
        out.append({**m, "content": content})
    return out
