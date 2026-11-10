"""会话管理路由（需求 §7.2 / F-10 / 架构 §3.4）。

会话增删查；删除会话在 chat_store 单事务内先删 chat_record 再删 chat_session（显式删除风格）。
SQL 全部位于 services/chat_store（R2）；本层只做编排与统一包络。
DB 不可用时 chat_store.get_pool() 抛 BizError(50001/503)，由全局异常处理器转规范包络。
"""
from __future__ import annotations

from fastapi import APIRouter

from errors import ok
from schemas.chat import SessionCreate
from services import chat_store

router = APIRouter(prefix="/api/chat/session", tags=["session"])


@router.post("")
async def create_session(body: SessionCreate):
    """POST /api/chat/session —— 新建会话，返回 {session_id, session_name, create_time}。"""
    item = await chat_store.create_session(body.session_name)
    return ok(item, "会话已创建")


@router.get("/list")
async def list_sessions():
    """GET /api/chat/session/list —— 按创建时间倒序列出（含 record_count 冗余计数）。"""
    items = await chat_store.list_sessions()
    return ok({"total": len(items), "list": items})


@router.delete("/{session_id}")
async def delete_session(session_id: int):
    """DELETE /api/chat/session/{session_id} —— 删除会话及其全部记录；不存在返回 40402。"""
    await chat_store.delete_session(session_id)
    return ok({"session_id": session_id}, "会话已删除")
