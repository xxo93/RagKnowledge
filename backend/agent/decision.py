"""Agent 意图决策。

落地：**关键词兜底决策**（离线、确定性、可单测），等价于"LLM 不可用 / 解析失败"时的降级路径，保证状态图不依赖 LLM 即可完成三类问题的工具调度。
启用：`llm_plan_decision`（OpenAI 兼容 `response_format=json_object`）作为首选，其失败/非法时回落到 `keyword_decision`。

决策契约：{"tool": "knowledge_search|calculator|doc_stats|None", "args": dict, "reason": str}
"""
from __future__ import annotations

import json
import logging
import re

from prompts import CALC_EXPR_PATTERN, DOC_STATS_KEYWORDS
from config import settings

logger = logging.getLogger("rag.agent.decision")

# stat_type 中文语义 -> 枚举白名单（与 vector_store._STAT_SQL / doc_stats_tool.ALLOWED_STATS 对齐）
_STAT_HINTS = (
    ("failed_docs", ("失败", "失败文档")),
    ("success_docs", ("成功", "成功文档")),
    ("total_chunks", ("切片", "chunk", "分段")),
)


def infer_stat_type(query: str) -> str:
    """从统计类问句推断统计枚举；无法细分时默认文档总数 total_docs。"""
    q = query
    for stat_type, hints in _STAT_HINTS:
        if any(h in q for h in hints):
            return stat_type
    return "total_docs"


def keyword_decision(query: str) -> dict:
    """纯规则兜底决策（不联网、无外部依赖）。

    1. 仅由数字/四则运算符/括号/空白组成且含数字 → calculator；
    2. 含统计关键词（多少份/统计/数量/条数/失败/切片数等）→ doc_stats；
    3. 其余 → knowledge_search（默认知识库检索）。
    """
    q = (query or "").strip()

    if re.match(CALC_EXPR_PATTERN, q) and any(ch.isdigit() for ch in q):
        return {
            "tool": "calculator",
            "args": {"expression": q},
            "reason": "纯数字与四则运算，命中计算器关键词兜底",
        }

    if any(k in q for k in DOC_STATS_KEYWORDS):
        return {
            "tool": "doc_stats",
            "args": {"stat_type": infer_stat_type(q)},
            "reason": "含统计关键词，命中文档统计工具兜底",
        }

    return {
        "tool": "knowledge_search",
        "args": {"query": q},
        "reason": "非算式、非统计，默认走知识库检索兜底",
    }


def llm_plan_decision(query: str) -> dict | None:
    """尝试 LLM 结构化决策；不可用或解析失败时返回 None，由调用方兜底。"""
    try:
        from llm import chat
        from prompts import PLAN_SYSTEM_PROMPT

        raw = chat(
            [
                {"role": "system", "content": PLAN_SYSTEM_PROMPT},
                {"role": "user", "content": query},
            ],
            json_mode=True,
        )
        data = json.loads(raw or "{}")
        tool = data.get("tool")
        if tool in ("knowledge_search", "calculator", "doc_stats"):
            return {
                "tool": tool,
                "args": data.get("args") or {},
                "reason": data.get("reason") or "LLM 结构化决策",
            }
        if tool in (None, "null"):
            return {"tool": None, "args": {}, "reason": "LLM 判定无需工具"}
        return None
    except Exception as exc:  # noqa: BLE001 - 决策降级，不击穿图
        logger.warning("LLM 意图决策失败，回落关键词兜底：%s", exc)
        return None


def decide(query: str) -> dict:
    """统一决策入口：LLM 决策失败或关闭时回退关键词规则。"""
    if settings.LLM_PLAN_ENABLED:
        llm = llm_plan_decision(query)
        if llm is not None:
            return llm
    return keyword_decision(query)
