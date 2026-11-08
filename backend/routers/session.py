"""会话管理路由（需求 §7.2 / F-10）。

会话增删查；删除会话时单事务先删 chat_record 再删 chat_session（架构 §3.4，显式删除风格）。
骨架阶段固定路径与入参契约，SQL 在 M4 经仓储接通。
"""
from __future__ import annotations

from fastapi import APIRouter

from errors import BizError, ok
from schemas.chat import SessionCreate

router = APIRouter(prefix="/api/chat/session", tags=["session"])


@router.post("")
async def create_session(body: SessionCreate):
    """POST /api/chat/session —— 新建会话，前端传名，默认“新会话”（M4 接通）。"""
    raise BizError(50100, "会话创建将在 M4 阶段接通", http_status=501)


@router.get("/list")
async def list_sessions():
    """GET /api/chat/session/list —— 按创建时间倒序列出（M4 接通）。"""
    raise BizError(50100, "会话列表将在 M4 阶段接通", http_status=501)


@router.delete("/{session_id}")
async def delete_session(session_id: int):
    """DELETE /api/chat/session/{session_id} —— 删除会话及其全部记录（M4 接通）。"""
    raise BizError(50100, "会话删除将在 M4 阶段接通", http_status=501)
