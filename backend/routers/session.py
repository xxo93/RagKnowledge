"""会话管理路由（多用户隔离：仅当前用户可操作本人会话）。

SQL 全部位于 services/chat_store；本层只做编排与统一包络。
越权与不存在统一返回 40402，不区分二者，避免通过错误码探测他人会话是否存在。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from errors import ok
from schemas.chat import SessionCreate, SessionRename
from services import chat_store
from services.auth_deps import get_current_user

router = APIRouter(prefix="/api/chat/session", tags=["session"])


@router.post("")
async def create_session(body: SessionCreate, user: dict = Depends(get_current_user)):
    """POST /api/chat/session —— 新建当前用户私有会话。"""
    item = await chat_store.create_session(user["user_id"], body.session_name)
    return ok(item, "会话已创建")


@router.get("/list")
async def list_sessions(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    keyword: Optional[str] = Query(None, max_length=128),
    user: dict = Depends(get_current_user),
):
    """GET /api/chat/session/list?page=&size=&keyword= —— 只返回当前用户会话及记录数。"""
    offset = (page - 1) * size
    total, items = await chat_store.list_sessions(
        user["user_id"], limit=size, offset=offset, keyword=keyword
    )
    return ok({"total": total, "list": items, "page": page, "size": size, "keyword": keyword})


@router.patch("/{session_id}")
async def rename_session(
    session_id: int,
    body: SessionRename,
    user: dict = Depends(get_current_user),
):
    """PATCH /api/chat/session/{session_id} —— 重命名本人会话；越权返回 40402。"""
    item = await chat_store.rename_session(session_id, user["user_id"], body.session_name)
    return ok(item, "会话已重命名")


@router.delete("/{session_id}")
async def delete_session(session_id: int, user: dict = Depends(get_current_user)):
    """DELETE /api/chat/session/{session_id} —— 删除本人会话及其记录；越权返回 40402。"""
    await chat_store.delete_session(session_id, user["user_id"])
    return ok({"session_id": session_id}, "会话已删除")
