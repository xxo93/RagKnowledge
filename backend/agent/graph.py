"""LangGraph Agent 状态图编排。

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
from typing import AsyncIterator, Awaitable, Callable, Optional

from .state import AgentState, initial_state
from . import decision as decision_mod, dedup, replan
from .tools import search_tool, calc_tool, doc_stats_tool, biz_query_tool
from ..config import settings
from ..prompts import NO_RESULT_FALLBACK, RUNNING_LABELS, DONE_LABELS

logger = logging.getLogger("rag.graph")

# doc_stats 枚举 -> 中文展示名（answer 节点汇总用）
_STAT_LABEL = {
    "total_docs": "文档总数",
    "success_docs": "成功文档数",
    "failed_docs": "失败文档数",
    "total_chunks": "切片总数",
}

EventSink = Optional[Callable[[dict], Awaitable[None]]]


async def _emit(state: AgentState, event: dict) -> None:
    """把工具生命周期事件投递到可选回调；无回调时保持纯状态图行为不变。"""
    sink = state.get("event_sink")
    if sink is not None:
        await sink(event)


def _done_label(tool_name: str, result_count: Optional[int], detail: Optional[dict] = None) -> str:
    base = DONE_LABELS.get(tool_name, tool_name)
    detail = detail or {}
    if tool_name == "knowledge_search" and result_count is not None:
        return f"{base} · 命中 {result_count} 条来源"
    if tool_name == "biz_query":
        if detail.get("title") and detail.get("rows") is not None:
            suffix = f" · {detail.get('title')} {detail.get('rows')} 行"
            if detail.get("truncated"):
                suffix += f"（共 {detail.get('total')} 行，已截断）"
            return base + suffix
        return f"{base} · 未执行"
    if tool_name == "calculator" and detail.get("result") is not None:
        expression = detail.get("expression") or "算式"
        return f"{base}：{expression} = {detail.get('result')}"
    if tool_name == "doc_stats" and detail.get("value") is not None:
        label = _STAT_LABEL.get(detail.get("stat_type"), detail.get("stat_type") or "统计")
        return f"{base}：{label} {detail.get('value')}"
    return base


# ---------------- 节点 ----------------
async def plan_node(state: AgentState) -> dict:
    """执行意图决策；同步决策调用放入线程，避免阻塞事件循环。"""
    decision = await asyncio.to_thread(decision_mod.decide, state.get("query", ""))
    return {"decision": decision}


async def knowledge_search_node(state: AgentState) -> dict:
    """执行知识库检索工具，回写观察、工具调用记录与去重来源；重复调用直接跳过。"""
    args = (state.get("decision") or {}).get("args") or {}
    query = args.get("query") or state.get("query", "")
    tool_name = "knowledge_search"
    old_keys = state.get("used_keys") or []
    key = dedup.call_key(tool_name, args)
    if dedup.is_duplicate(old_keys, tool_name, args):
        logger.info("跳过重复工具调用：%s", key)
        return {"used_keys": old_keys}

    round_no = state.get("round", 0)
    await _emit(state, {
        "type": "tool", "name": tool_name, "status": "running",
        "label": RUNNING_LABELS[tool_name], "round": round_no,
    })

    result = await search_tool.run(query)
    result_count = len(result.get("sources") or [])
    done_label = _done_label(tool_name, result_count)
    await _emit(state, {
        "type": "tool", "name": tool_name, "status": "done", "round": round_no,
        "label": done_label, "result_count": result_count, "ok": bool(result.get("ok")),
        "error": result.get("error"),
    })

    observation = {
        "round": round_no,
        "tool": tool_name,
        "ok": bool(result.get("ok")),
        "result": result,
        "error": result.get("error"),
    }
    tool_call = {
        "name": tool_name,
        "label": done_label,
        "status": "done",
        "round": round_no,
        "result_count": result_count,
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
    """执行计算器；纯 CPU 求值放入线程；重复表达式直接跳过。"""
    args = (state.get("decision") or {}).get("args") or {}
    expression = args.get("expression") or ""
    tool_name = "calculator"
    old_keys = state.get("used_keys") or []
    key = dedup.call_key(tool_name, args)
    if dedup.is_duplicate(old_keys, tool_name, args):
        logger.info("跳过重复工具调用：%s", key)
        return {"used_keys": old_keys}

    round_no = state.get("round", 0)
    await _emit(state, {
        "type": "tool", "name": tool_name, "status": "running",
        "label": RUNNING_LABELS[tool_name], "round": round_no,
    })

    result = await asyncio.to_thread(calc_tool.calculate, expression)
    detail = {"expression": result.get("expression"), "result": result.get("result")}
    done_label = _done_label(tool_name, 1 if result.get("ok") else None, detail)
    await _emit(state, {
        "type": "tool", "name": tool_name, "status": "done", "round": round_no,
        "label": done_label, "result_count": 1 if result.get("ok") else None,
        "ok": bool(result.get("ok")), "error": result.get("error"),
        "detail": detail,
    })

    observation = {
        "round": round_no,
        "tool": tool_name,
        "ok": bool(result.get("ok")),
        "result": detail,
        "error": result.get("error"),
    }
    tool_call = {
        "name": tool_name,
        "label": done_label,
        "status": "done",
        "round": round_no,
        "result_count": 1 if result.get("ok") else None,
        "detail": detail,
    }
    return {
        "observations": (state.get("observations") or []) + [observation],
        "tool_calls": (state.get("tool_calls") or []) + [tool_call],
        "used_keys": old_keys + [key],
    }


async def doc_stats_node(state: AgentState) -> dict:
    """执行文档统计工具；统计类型使用白名单；重复统计直接跳过。"""
    args = (state.get("decision") or {}).get("args") or {}
    stat_type = args.get("stat_type") or ""
    tool_name = "doc_stats"
    old_keys = state.get("used_keys") or []
    key = dedup.call_key(tool_name, args)
    if dedup.is_duplicate(old_keys, tool_name, args):
        logger.info("跳过重复工具调用：%s", key)
        return {"used_keys": old_keys}

    round_no = state.get("round", 0)
    await _emit(state, {
        "type": "tool", "name": tool_name, "status": "running",
        "label": RUNNING_LABELS[tool_name], "round": round_no,
    })

    result = await doc_stats_tool.run(stat_type)
    value = result.get("value")
    result_count = int(value) if isinstance(value, (int, float)) else None
    detail = {"stat_type": result.get("stat_type"), "value": value}
    done_label = _done_label(tool_name, result_count, detail)
    await _emit(state, {
        "type": "tool", "name": tool_name, "status": "done", "round": round_no,
        "label": done_label, "result_count": result_count,
        "ok": bool(result.get("ok")), "error": result.get("error"),
        "detail": detail,
    })

    observation = {
        "round": round_no,
        "tool": tool_name,
        "ok": bool(result.get("ok")),
        "result": detail,
        "error": result.get("error"),
    }
    tool_call = {
        "name": tool_name,
        "label": done_label,
        "status": "done",
        "round": round_no,
        "result_count": result_count,
        "detail": detail,
    }
    return {
        "observations": (state.get("observations") or []) + [observation],
        "tool_calls": (state.get("tool_calls") or []) + [tool_call],
        "used_keys": old_keys + [key],
    }


async def biz_query_node(state: AgentState) -> dict:
    """执行双业务库查询；权限/参数/合规/脱敏/行数限制均在 services.biz_query 内完成。"""
    args = (state.get("decision") or {}).get("args") or {}
    query = args.get("query") or state.get("query", "")
    tool_name = "biz_query"
    old_keys = state.get("used_keys") or []
    key = dedup.call_key(tool_name, {"query": query})
    if dedup.is_duplicate(old_keys, tool_name, {"query": query}):
        logger.info("跳过重复工具调用：%s", key)
        return {"used_keys": old_keys}

    round_no = state.get("round", 0)
    await _emit(state, {
        "type": "tool", "name": tool_name, "status": "running",
        "label": RUNNING_LABELS[tool_name], "round": round_no,
    })

    result = await biz_query_tool.run(query, state.get("user") or {})
    table = result.get("table") or {}
    detail = {
        "intent": result.get("intent"),
        "title": table.get("title"),
        "rows": len(table.get("rows") or []) if table else None,
        "total": table.get("total"),
        "truncated": table.get("truncated"),
        "code": result.get("code"),
    }
    result_count = detail["rows"]
    done_label = _done_label(tool_name, result_count, detail)
    await _emit(state, {
        "type": "tool", "name": tool_name, "status": "done", "round": round_no,
        "label": done_label, "result_count": result_count, "ok": bool(result.get("ok")),
        "error": None if result.get("ok") else result.get("message"),
        "detail": detail,
    })

    observation = {
        "round": round_no,
        "tool": tool_name,
        "ok": bool(result.get("ok")),
        "result": result,
        "error": None if result.get("ok") else result.get("message"),
    }
    tool_call = {
        "name": tool_name,
        "label": done_label,
        "status": "done",
        "round": round_no,
        "result_count": result_count,
        "detail": detail,
    }
    return {
        "observations": (state.get("observations") or []) + [observation],
        "tool_calls": (state.get("tool_calls") or []) + [tool_call],
        "used_keys": old_keys + [key],
    }


async def assess_node(state: AgentState) -> dict:
    """每次工具执行后累加轮次；轮次上限由 replan 收敛规则控制。"""
    new_round = state.get("round", 0) + 1
    return {"round": new_round}


async def replan_node(state: AgentState) -> dict:
    """执行再规划判断；默认收敛到答案生成，开启 LLM 再决策时可回跳工具节点。"""
    route, new_decision = replan.plan_next(state)
    if new_decision is None:
        return {"replan_route": None}
    return {"decision": new_decision, "replan_route": route}


async def answer_node(state: AgentState) -> dict:
    """汇总确定性工具观察结果。"""
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
        elif tool == "biz_query":
            if ok:
                table = result.get("table") or {}
                parts.append(
                    f"{table.get('title') or '业务查询'}：共 {table.get('total')} 行"
                    f"{'（已截断，仅展示前 %d 行）' % len(table.get('rows') or []) if table.get('truncated') else ''}"
                )
            else:
                parts.append(result.get("message") or "业务库查询未执行")

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
    if tool == "biz_query":
        return "biz_query"
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
    """构建并编译 LangGraph 状态图。"""
    from langgraph.graph import StateGraph, END

    g = StateGraph(AgentState)
    g.add_node("plan", plan_node)
    g.add_node("knowledge_search", knowledge_search_node)
    g.add_node("calculator", calculator_node)
    g.add_node("doc_stats", doc_stats_node)
    g.add_node("biz_query", biz_query_node)
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
            "biz_query": "biz_query",
            "answer": "answer",
        },
    )
    g.add_edge("knowledge_search", "assess")
    g.add_edge("calculator", "assess")
    g.add_edge("doc_stats", "assess")
    g.add_edge("biz_query", "assess")
    g.add_edge("assess", "replan")
    g.add_conditional_edges(
        "replan",
        route_after_replan,
        {
            "knowledge_search": "knowledge_search",
            "calculator": "calculator",
            "doc_stats": "doc_stats",
            "biz_query": "biz_query",
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
async def run_once(
    query: str,
    history: list[dict] | None = None,
    on_event: EventSink = None,
    user: dict | None = None,
) -> dict:
    """非流式执行完整状态图，返回最终状态字典；on_event 可选接收工具生命周期事件。"""
    graph = get_graph()
    return await graph.ainvoke(initial_state(query, history, event_sink=on_event, user=user))


async def run_stream(
    query: str,
    history: list[dict] | None = None,
    session_id: int | None = None,
) -> AsyncIterator[dict]:
    """输出 Agent 执行过程事件；事件类型包括 tool、source、done、error。"""
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

    yield {
        "type": "done",
        "record_id": None,
        "session_id": session_id,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
    }
