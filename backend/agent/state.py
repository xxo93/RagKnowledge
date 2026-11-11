"""Agent 状态结构定义。

使用 TypedDict；LangGraph 默认按 key 覆盖，observations / tool_calls 由节点内追加后整体返回，避免并发写冲突。
"""
from __future__ import annotations

from typing import Awaitable, Callable, Optional, TypedDict


class Decision(TypedDict, total=False):
    """plan 节点的结构化意图判断结果。"""
    tool: Optional[str]            # knowledge_search / calculator / doc_stats / None
    args: dict
    reason: str


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
) -> AgentState:
    """构造初始状态（round=0，其余列表为空）。"""
    return AgentState(
        query=query,
        history=history or [],
        round=0,
        decision=None,
        observations=[],
        tool_calls=[],
        sources=[],
        used_keys=[],
        answer="",
        error=None,
        finished=False,
        event_sink=event_sink,
    )
