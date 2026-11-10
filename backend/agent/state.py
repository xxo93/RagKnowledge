"""Agent 状态定义（架构 §4.1）。

使用 TypedDict；LangGraph 默认按 key 覆盖，observations / tool_calls 由节点内追加后整体返回，
避免并发写冲突。字段契约与 §4.1 表一一对应。
"""
from __future__ import annotations

from typing import TypedDict, Optional


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


class SourceRef(TypedDict, total=False):
    doc_name: str
    chunk_index: int
    score: float
    snippet: str


class AgentState(TypedDict, total=False):
    query: str
    history: list[dict]
    round: int
    decision: Optional[Decision]
    observations: list[Observation]
    tool_calls: list[ToolCallRecord]
    sources: list[SourceRef]
    used_keys: list[str]           # 已执行工具的防重复调用键（tool|规范化args），§4.2 再规划保护
    answer: str
    error: Optional[str]
    finished: bool
    replan_route: Optional[str]     # replan 节点计算出的下一工具路由（无回跳时为 None）


def initial_state(query: str, history: list[dict] | None = None) -> AgentState:
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
    )
