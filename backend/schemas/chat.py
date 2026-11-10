"""会话、聊天与 SSE 事件的数据模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class SessionCreate(BaseModel):
    session_name: str = Field(default="新会话", max_length=128)


class SessionRename(BaseModel):
    session_name: str = Field(min_length=1, max_length=128)


class SessionItem(BaseModel):
    session_id: int
    session_name: str
    create_time: datetime | None = None


class ChatStreamRequest(BaseModel):
    session_id: int
    query: str = Field(min_length=1, max_length=2000)


class RecordItem(BaseModel):
    record_id: int
    query: str
    answer: str
    tool_call_info: Optional[dict[str, Any]] = None
    source_ref: Optional[dict[str, Any]] = None
    create_time: datetime | None = None


# ---- SSE 事件载荷；前端按事件类型分发渲染 ----
class ToolEvent(BaseModel):
    name: str
    label: str
    status: str                 # running / done
    round: int


class TokenEvent(BaseModel):
    text: str


class SourceItem(BaseModel):
    doc_name: str
    chunk_index: int
    score: float
    snippet: str


class SourceEvent(BaseModel):
    sources: list[SourceItem]


class DoneEvent(BaseModel):
    record_id: int | None
    session_id: int
    elapsed_ms: int


class ErrorEvent(BaseModel):
    code: str
    msg: str
