"""会话、聊天与 SSE 事件的数据模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class SessionCreate(BaseModel):
    session_name: str = Field(default="新会话", max_length=128)


class SessionRename(BaseModel):
    session_name: str = Field(min_length=1, max_length=128)


class SessionPinUpdate(BaseModel):
    """置顶开关（M8 / WP5-9）。"""

    pinned: bool


class SessionArchiveUpdate(BaseModel):
    """归档/恢复开关（M8 / WP5-9）。"""

    archived: bool = True


class SessionBatchAction(BaseModel):
    """v4 / N6：批量置顶/归档/恢复；不含删除。"""

    action: Literal["pin", "unpin", "archive", "restore"]
    session_ids: list[int] = Field(min_length=1, max_length=50)


class MemorySettings(BaseModel):
    """会话记忆开关（临时会话 = memory_enabled false）。"""

    memory_enabled: bool


class SessionItem(BaseModel):
    session_id: int
    session_name: str
    create_time: datetime | None = None


class ChatStreamRequest(BaseModel):
    session_id: int
    query: str = Field(min_length=1, max_length=2000)
    # v3 / WP2：模式选择器。auto＝按意图自动路由；knowledge＝仅知识库；general＝仅通用；
    # biz＝业务库；workflow＝任务（WP4 开放，本期固定提示）
    mode: str = Field(default="auto", pattern="^(auto|knowledge|general|biz|workflow)$")


class FeedbackCreate(BaseModel):
    """用户对一条回答的赞/踩反馈（v3 / WP5-3）。"""

    record_id: int
    rating: Literal[1, -1] = Field(description="1=有帮助，-1=需改进")
    reason_code: str | None = Field(default=None, max_length=32)
    reason_text: str | None = Field(default=None, max_length=500)


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
    result_count: int | None = None
    ok: bool | None = None
    error: str | None = None
    detail: dict[str, object] | None = None


class TokenEvent(BaseModel):
    text: str


class SourceItem(BaseModel):
    doc_name: str
    doc_title: str | None = None
    chunk_id: int | None = None
    doc_id: int | None = None
    chunk_index: int
    score: float
    snippet: str
    summary: str | None = None
    excerpt: str | None = None
    rerank_score: float | None = None


class SourceEvent(BaseModel):
    sources: list[SourceItem]


class DoneEvent(BaseModel):
    record_id: int | None
    session_id: int
    elapsed_ms: int
    session_title: str | None = None
    source_count: int | None = None


class ErrorEvent(BaseModel):
    code: str
    msg: str
