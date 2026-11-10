"""LangGraph 状态图编排（F-06 / 架构 §4.2 / ADR-003）。

状态图（M3 落地）：
    START -> plan(意图判断，关键词兜底；LLM 结构化决策预留 M4)
              ├ tool=None        -> answer
              ├ knowledge_search -> assess
              ├ calculator       -> assess
              └ doc_stats        -> assess
    assess -> answer（M3 单轮收敛；轮次上限 MAX_TOOL_ROUNDS 与防重复逻辑就位，多轮再规划随 M4）
    answer -> END

M3 边界：
- 落地真实 StateGraph + 三工具接线 + observation/tool_calls/sources 状态回写 + 收敛；
- answer 节点做**确定性汇总**（不调 LLM）；token 流式、真实 LLM 决策与生成、SSE 落库属 M4；
- 列表型状态（observations/tool_calls/sources）无 reducer，节点内读旧值追加后整体返回（§4.1 注释约定）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator

from agent.state import AgentState, initial_state
from agent import decision as decision_mod, dedup, replan
from agent.tools import search_tool, calc_tool, doc_stats_tool
from config import settings
from prompts import NO_RESULT_FALLBACK, TOOL_LABELS

logger = logging.getLogger("rag.graph")

# doc_stats 枚举 -> 中文展示名（answer 节点汇总用）
_STAT_LABEL = {
    "total_docs": "文档总数",
    "success_docs": "成功文档数",
    "failed_docs": "失败文档数",
    "total_chunks": "切片总数",
}


# ---------------- 节点 ----------------
async def plan_node(state: AgentState) -> dict:
    """意图判断（M4）：先 LLM 结构化决策（受 LLM_PLAN_ENABLED 控制），失败/非法或关闭时
    回落关键词兜底（架构 §4.3）；同步决策调用经 to_thread 避免阻塞事件循环。"""
    decision = await asyncio.to_thread(decision_mod.decide, state.get("query", ""))
    return {"decision": decision}


async def knowledge_search_node(state: AgentState) -> dict:
    """调 knowledge_search 工具；回写 observation、tool_calls、去重后的 sources。
    防重复：同一 (tool,args) 再规划命中时跳过重复执行（§4.2 dedup 保护）。"""
    args = (state.get("decision") or {}).get("args") or {}
    query = args.get("query") or state.get("query", "")
    tool_name = "knowledge_search"
    old_keys = state.get("used_keys") or []
    key = dedup.call_key(tool_name, args)
    if dedup.is_duplicate(old_keys, tool_name, args):
        logger.info("跳过重复工具调用：%s", key)
        return {"used_keys": old_keys}

    result = await search_tool.run(query)

    round_no = state.get("round", 0)
    observation = {
        "round": round_no,
        "tool": "knowledge_search",
        "ok": bool(result.get("ok")),
        "result": result,
        "error": result.get("error"),
    }
    tool_call = {
        "name": "knowledge_search",
        "label": TOOL_LABELS["knowledge_search"],
        "status": "done",
        "round": round_no,
    }

    sources = list(state.get("sources") or [])
    seen = {(s.get("doc_name"), s.get("chunk_index")) for s in sources}
    for s in (result.get("sources") or []):
        key_src = (s.get("doc_name"), s.get("chunk_index"))
        if key_src not in seen:
            seen.add(key_src)
            sources.append(s)

    return {
        "observations": (state.get("observations") or []) + [observation],
        "tool_calls": (state.get("tool_calls") or []) + [tool_call],
        "sources": sources,
        "used_keys": old_keys + [key],
    }


async def calculator_node(state: AgentState) -> dict:
    """调 calculator 工具；纯 CPU 求值经 to_thread 避免阻塞事件循环。
    防重复：同一表达式重复调用时跳过（§4.2 dedup 保护）。"""
    args = (state.get("decision") or {}).get("args") or {}
    expression = args.get("expression") or ""
    tool_name = "calculator"
    old_keys = state.get("used_keys") or []
    key = dedup.call_key(tool_name, args)
    if dedup.is_duplicate(old_keys, tool_name, args):
        logger.info("跳过重复工具调用：%s", key)
        return {"used_keys": old_keys}

    result = await asyncio.to_thread(calc_tool.calculate, expression)

    round_no = state.get("round", 0)
    observation = {
        "round": round_no,
        "tool": "calculator",
        "ok": bool(result.get("ok")),
        "result": {"expression": result.get("expression"), "result": result.get("result")},
        "error": result.get("error"),
    }
    tool_call = {
        "name": "calculator",
        "label": TOOL_LABELS["calculator"],
        "status": "done",
        "round": round_no,
    }
    return {
        "observations": (state.get("observations") or []) + [observation],
        "tool_calls": (state.get("tool_calls") or []) + [tool_call],
        "used_keys": old_keys + [key],
    }


async def doc_stats_node(state: AgentState) -> dict:
    """调 doc_stats 工具（枚举白名单 + 参数化聚合，不拼接 SQL）。
    防重复：同一统计维度重复调用时跳过（§4.2 dedup 保护）。"""
    args = (state.get("decision") or {}).get("args") or {}
    stat_type = args.get("stat_type") or ""
    tool_name = "doc_stats"
    old_keys = state.get("used_keys") or []
    key = dedup.call_key(tool_name, args)
    if dedup.is_duplicate(old_keys, tool_name, args):
        logger.info("跳过重复工具调用：%s", key)
        return {"used_keys": old_keys}

    result = await doc_stats_tool.run(stat_type)

    round_no = state.get("round", 0)
    observation = {
        "round": round_no,
        "tool": "doc_stats",
        "ok": bool(result.get("ok")),
        "result": {"stat_type": result.get("stat_type"), "value": result.get("value")},
        "error": result.get("error"),
    }
    tool_call = {
        "name": "doc_stats",
        "label": TOOL_LABELS["doc_stats"],
        "status": "done",
        "round": round_no,
    }
    return {
        "observations": (state.get("observations") or []) + [observation],
        "tool_calls": (state.get("tool_calls") or []) + [tool_call],
        "used_keys": old_keys + [key],
    }


async def assess_node(state: AgentState) -> dict:
    """轮次计数。每次工具执行后 round+1；上限语义由 replan 门槛收敛保证（F-06）。"""
    new_round = state.get("round", 0) + 1
    return {"round": new_round}


async def replan_node(state: AgentState) -> dict:
    """assess 之后的再规划节点：计算路由与（可选）带新 args 的决策。

    默认 `LLM_REPLAN_ENABLED=False` 时 `plan_next` 恒收敛、不写 decision，行为与单轮一致、
    零额外外部调用；开启后回跳工具前把新 `decision` 写回状态，工具节点据此取新参数执行。
    """
    route, new_decision = replan.plan_next(state)
    if new_decision is None:
        return {"replan_route": None}
    return {"decision": new_decision, "replan_route": route}


async def answer_node(state: AgentState) -> dict:
    """确定性汇总观察结果（M3 不调 LLM）；真实流式生成在 M4。"""
    observations = state.get("observations") or []
    sources = state.get("sources") or []
    parts: list[str] = []

    for obs in observations:
        tool = obs.get("tool")
        ok = obs.get("ok")
        result = obs.get("result") or {}
        if tool == "calculator":
            if ok:
                parts.append(f"计算结果：{result.get('expression')} = {result.get('result')}")
            else:
                parts.append(f"计算未通过校验：{obs.get('error')}")
        elif tool == "doc_stats":
            if ok:
                label = _STAT_LABEL.get(result.get("stat_type"), result.get("stat_type"))
                parts.append(f"{label}：{result.get('value')}")
            else:
                parts.append(f"统计失败：{obs.get('error')}")
        elif tool == "knowledge_search":
            if ok and sources:
                parts.append(f"已检索到 {len(sources)} 条相关来源（M4 将由 LLM 结合来源生成自然语言答案）")
            elif ok:
                parts.append(NO_RESULT_FALLBACK)
            else:
                parts.append(f"检索失败：{obs.get('error')}")

    if not parts:
        answer = "你好，我可以帮你检索知识库、做简单四则运算，或统计文档数量，直接告诉我你的问题即可。"
    else:
        answer = "\n".join(parts)

    return {"answer": answer, "finished": True}


# ---------------- 条件路由 ----------------
def route_after_plan(state: AgentState) -> str:
    """plan 之后按 decision.tool 选择下一节点。"""
    decision = state.get("decision") or {}
    tool = decision.get("tool")
    if tool == "knowledge_search":
        return "knowledge_search"
    if tool == "calculator":
        return "calculator"
    if tool == "doc_stats":
        return "doc_stats"
    return "answer"


def route_after_replan(state: AgentState) -> str:
    """replan 节点之后：按本次再规划路由回跳工具或收敛 answer（默认 answer）。

    路由由 `agent.replan.plan_next` 产出，受 MULTI_ROUND_ENABLED / MAX_TOOL_ROUNDS /
    观察成败 / dedup 重复键 / LLM_REPLAN_ENABLED 共同约束；工具节点另有 (tool,args) 防重复保护。
    """
    return state.get("replan_route") or "answer"


# ---------------- 图构建 ----------------
_compiled_graph = None


def build_graph():
    """构建并编译 StateGraph（真实 langgraph；延迟导入避免骨架启动强依赖）。"""
    from langgraph.graph import StateGraph, END

    g = StateGraph(AgentState)
    g.add_node("plan", plan_node)
    g.add_node("knowledge_search", knowledge_search_node)
    g.add_node("calculator", calculator_node)
    g.add_node("doc_stats", doc_stats_node)
    g.add_node("assess", assess_node)
    g.add_node("replan", replan_node)
    g.add_node("answer", answer_node)

    g.set_entry_point("plan")
    g.add_conditional_edges(
        "plan",
        route_after_plan,
        {
            "knowledge_search": "knowledge_search",
            "calculator": "calculator",
            "doc_stats": "doc_stats",
            "answer": "answer",
        },
    )
    g.add_edge("knowledge_search", "assess")
    g.add_edge("calculator", "assess")
    g.add_edge("doc_stats", "assess")
    g.add_edge("assess", "replan")
    g.add_conditional_edges(
        "replan",
        route_after_replan,
        {
            "knowledge_search": "knowledge_search",
            "calculator": "calculator",
            "doc_stats": "doc_stats",
            "answer": "answer",
        },
    )
    g.add_edge("answer", END)
    return g.compile()


def get_graph():
    """编译结果单例复用。"""
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_graph()
    return _compiled_graph


# ---------------- 运行入口 ----------------
async def run_once(query: str, history: list[dict] | None = None) -> dict:
    """非流式执行整图，返回最终 AgentState（dict）。供集成脚本与 M4 路由复用。"""
    graph = get_graph()
    return await graph.ainvoke(initial_state(query, history))


async def run_stream(
    query: str,
    history: list[dict] | None = None,
    session_id: int | None = None,
) -> AsyncIterator[dict]:
    """统一事件出口（§4.6 / §6.3）。M3 产出 tool / source / done；
    token 事件（LLM 流式）与 record_id 落库在 M4 补齐。

    事件字典：
        {"type":"tool","name","label","status","round"}
        {"type":"source","sources":[{doc_name,chunk_index,score,snippet}]}
        {"type":"done","record_id","session_id","elapsed_ms"}
        {"type":"error","code","msg"}
    """
    t0 = time.monotonic()
    try:
        final = await run_once(query, history)
    except Exception as exc:  # noqa: BLE001 - 致命错误映射为 error 事件，不击穿 SSE
        logger.exception("Agent 执行致命错误")
        yield {"type": "error", "code": "AGENT_RUNTIME_ERROR", "msg": str(exc)}
        return

    for tc in final.get("tool_calls") or []:
        yield {
            "type": "tool",
            "name": tc.get("name"),
            "label": tc.get("label"),
            "status": tc.get("status"),
            "round": tc.get("round"),
        }

    sources = final.get("sources") or []
    if sources:
        yield {"type": "source", "sources": sources}

    # TODO(M4): answer 节点接入 llm.chat_stream，在此逐 token 产出 {"type":"token","text":...}
    yield {
        "type": "done",
        "record_id": None,
        "session_id": session_id,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
    }
