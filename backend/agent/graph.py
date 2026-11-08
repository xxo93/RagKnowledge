"""LangGraph 状态图编排（F-06 / 架构 §4.2 / ADR-003）。

状态图（M3 实现）：
    START -> plan(意图判断)
              ├ tool=None          -> answer
              ├ knowledge_search   -> assess
              ├ calculator         -> assess
              └ doc_stats          -> assess
    assess -> need_more 且 round<MAX_TOOL_ROUNDS -> 下一工具；否则 -> answer -> END

设计要点：
- 框架依赖（langgraph）延迟导入，避免骨架阶段未安装时影响服务启动；
- 流式事件映射（§4.6）收敛在 run_stream() 内，路由层只消费统一事件字典；
- 循环上限 MAX_TOOL_ROUNDS=3（配置项），超限强制收敛到 answer；
- 连续两轮相同 (tool, args) 判重，避免无意义循环（§4.3）。
"""
from __future__ import annotations

import logging
from typing import AsyncIterator

from agent.state import AgentState, initial_state
from config import settings

logger = logging.getLogger("rag.graph")


# ---------------- 节点（M3 填充；当前为契约占位） ----------------
async def plan_node(state: AgentState) -> dict:
    """意图判断：LLM 结构化输出 JSON 决策；解析失败走关键词兜底（§4.3）。"""
    raise NotImplementedError("plan 节点将在 M3 实现")


async def knowledge_search_node(state: AgentState) -> dict:
    """调用 knowledge_search 工具，追加 observation / sources / tool_calls。"""
    raise NotImplementedError("knowledge_search 节点将在 M3 实现")


async def calculator_node(state: AgentState) -> dict:
    """调用 calculator 工具。"""
    raise NotImplementedError("calculator 节点将在 M3 实现")


async def doc_stats_node(state: AgentState) -> dict:
    """调用 doc_stats 工具。"""
    raise NotImplementedError("doc_stats 节点将在 M3 实现")


async def assess_node(state: AgentState) -> dict:
    """判断是否还需调用工具；round+1；达到 MAX_TOOL_ROUNDS 强制收敛。"""
    raise NotImplementedError("assess 节点将在 M3 实现")


async def answer_node(state: AgentState) -> dict:
    """汇总观察结果流式生成答案；无工具结果时按普通对话回答。"""
    raise NotImplementedError("answer 节点将在 M3 实现")


# ---------------- 条件路由 ----------------
def route_after_plan(state: AgentState) -> str:
    """plan 之后按 decision.tool 选择下一节点名。"""
    decision = state.get("decision") or {}
    tool = decision.get("tool")
    if tool == "knowledge_search":
        return "knowledge_search"
    if tool == "calculator":
        return "calculator"
    if tool == "doc_stats":
        return "doc_stats"
    return "answer"


def route_after_assess(state: AgentState) -> str:
    """assess 之后：需要更多且未超限 -> 下一工具；否则 answer。"""
    if state.get("round", 0) >= settings.MAX_TOOL_ROUNDS:
        return "answer"
    decision = state.get("decision") or {}
    return decision.get("tool") or "answer"


# ---------------- 图构建与流式入口 ----------------
def build_graph():
    """构建并编译 StateGraph（M3）。

    实现提示：
        from langgraph.graph import StateGraph, END
        g = StateGraph(AgentState)
        g.add_node("plan", plan_node) ...
        g.set_entry_point("plan")
        g.add_conditional_edges("plan", route_after_plan, {...})
        ...
        return g.compile()
    """
    raise NotImplementedError("状态图将在 M3 组装")


async def run_stream(query: str, history: list[dict] | None = None) -> AsyncIterator[dict]:
    """统一流式事件出口（§4.6），产出字典事件：
    {"type": "tool", "name", "label", "status", "round"}
    {"type": "token", "text"}
    {"type": "source", "sources": [...]}
    {"type": "done", "record_id", "session_id", "elapsed_ms"}
    {"type": "error", "code", "msg"}
    路由层把这些字典转成 SSE 帧；框架事件 API 漂移时在此单点替换为队列方案。
    """
    raise NotImplementedError("run_stream 将在 M3/M4 实现")
