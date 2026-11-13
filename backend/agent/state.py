"""Agent 状态结构定义。

使用 TypedDict；LangGraph 默认按 key 覆盖，observations / tool_calls 由节点内追加后整体返回，避免并发写冲突。
"""
from __future__ import annotations

from typing import Awaitable, Callable, Optional, TypedDict


class Decision(TypedDict, total=False):
    """plan 节点的结构化意图判断结果（v3 / WP2 扩到 7 类意图）。"""
    intent: str                    # knowledge / biz_query / calculator / doc_stats / general / workflow / clarify
    tool: Optional[str]            # knowledge_search / calculator / doc_stats / biz_query / None
    args: dict
    confidence: float
    reason: str
    degraded_reason: Optional[str]  # general 被内部事实保护强制降级为 knowledge 的原因
    suppressed_intent: Optional[str]  # 显式"仅知识库"模式下被压制的通用意图


class Observation(TypedDict, total=False):
    round: int
    tool: str
    ok: bool
    result: object
    error: Optional[str]


class ToolCallRecord(TypedDict, total=False):
    name: str
    label: str
    status: str                     # running / done
    round: int
    result_count: Optional[int]
    detail: dict


class SourceRef(TypedDict, total=False):
    doc_name: str
    doc_title: str
    chunk_id: int
    doc_id: int
    chunk_index: int
    score: float
    snippet: str
    summary: str
    excerpt: str
    rerank_score: float


class AgentState(TypedDict, total=False):
    query: str
    history: list[dict]
    user: dict                     # 当前登录用户（user_id/role），业务库权限校验用
    round: int
    decision: Optional[Decision]
    observations: list[Observation]
    tool_calls: list[ToolCallRecord]
    sources: list[SourceRef]
    used_keys: list[str]           # 已执行工具的规范化调用键，用于重复调用保护
    answer: str
    error: Optional[str]
    finished: bool
    replan_route: Optional[str]     # 再规划选择的下一工具路由；无需回跳时为 None
    event_sink: Optional[Callable[[dict], Awaitable[None]]]  # 工具生命周期事件回调


def initial_state(
    query: str,
    history: list[dict] | None = None,
    event_sink: Optional[Callable[[dict], Awaitable[None]]] = None,
    user: dict | None = None,
    decision: Optional[Decision] = None,
) -> AgentState:
    """构造初始状态（round=0，其余列表为空）。

    decision 可由路由层预计算后传入（v3 / WP2 的显式模式与内部事实保护需要异步取词表），
    plan 节点检测到已有决策时直接复用，不再调用一次模型。
    """
    return AgentState(
        query=query,
        history=history or [],
        user=user or {},
        round=0,
        decision=decision,
        observations=[],
        tool_calls=[],
        sources=[],
        used_keys=[],
        answer="",
        error=None,
        finished=False,
        event_sink=event_sink,
    )
