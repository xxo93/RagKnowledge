"""会话记忆接口（v3 / WP1-4）。

路径与需求 §3.7 对齐：`/api/sessions/{session_id}/memory*`。
与现有 `/api/chat/session` 的区别只是前缀；归属校验、40402 语义、统一包络完全一致。

硬约束：
- **不提供任何接收 user_id 的记忆接口**；用户身份只从 Cookie 会话解析（get_current_user），
  越权与不存在统一 40402，不区分二者。
- 删除单条事实时同时校验 session 归属，避免拿别人会话的 mem_id 猜测越权。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..errors import BizError, ok, ERR_SESSION_NOT_FOUND
from ..schemas.chat import MemorySettings
from ..services import chat_store, memory, session_memory_store
from ..services.auth_deps import get_current_user

router = APIRouter(prefix="/api/sessions", tags=["session-memory"])

_404 = "会话不存在或无权访问"


def _ensure_session(state) -> dict:
    if state is None:
        raise BizError(ERR_SESSION_NOT_FOUND, _404, http_status=404)
    return state


@router.get("/{session_id}/memory")
async def get_memory(session_id: int, user: dict = Depends(get_current_user)):
    """GET /api/sessions/{session_id}/memory —— 事实 + 摘要 + 上下文用量。"""
    state = _ensure_session(await chat_store.get_memory_state(session_id, user["user_id"]))
    facts = await session_memory_store.list_facts(session_id, user["user_id"], limit=50)
    bundle = await memory.build_context(session_id, user["user_id"])
    return ok(
        {
            "session_id": session_id,
            "memory_enabled": state["memory_enabled"],
            "summary": state["summary"],
            "summarized_until_record_id": state["summarized_until_record_id"],
            "facts": facts,
            "context_usage": bundle.usage,
        }
    )


@router.delete("/{session_id}/memory")
async def clear_memory(session_id: int, user: dict = Depends(get_current_user)):
    """DELETE /api/sessions/{session_id}/memory —— 清空事实与摘要（水位推到最新）。"""
    state = _ensure_session(await chat_store.get_memory_state(session_id, user["user_id"]))
    removed = await session_memory_store.clear_facts(session_id, user["user_id"])
    await chat_store.reset_memory(session_id, user["user_id"])
    return ok(
        {"session_id": session_id, "removed_facts": removed, "summary_cleared": True},
        "会话记忆已清空",
    )


@router.get("/{session_id}/memory/{fact_id}/history")
async def get_fact_history(session_id: int, fact_id: int, user: dict = Depends(get_current_user)):
    """GET /api/sessions/{session_id}/memory/{fact_id}/history —— 事实多版口径时间线。"""
    _ensure_session(await chat_store.get_memory_state(session_id, user["user_id"]))
    rows = await session_memory_store.list_fact_history(session_id, user["user_id"], fact_id)
    if rows is None:
        raise BizError(ERR_SESSION_NOT_FOUND, "记忆条目不存在或已删除", http_status=404)
    return ok({"session_id": session_id, "fact_id": fact_id, "history": rows})


@router.delete("/{session_id}/memory/facts/{mem_id}")
async def delete_fact(session_id: int, mem_id: int, user: dict = Depends(get_current_user)):
    """DELETE /api/sessions/{session_id}/memory/facts/{mem_id} —— 删除单条事实。"""
    _ensure_session(await chat_store.get_memory_state(session_id, user["user_id"]))
    removed = await session_memory_store.invalidate_fact(session_id, user["user_id"], mem_id)
    if not removed:
        raise BizError(ERR_SESSION_NOT_FOUND, "记忆条目不存在或已删除", http_status=404)
    return ok({"session_id": session_id, "mem_id": mem_id}, "记忆条目已删除")


@router.put("/{session_id}/memory/settings")
async def update_settings(
    session_id: int,
    body: MemorySettings,
    user: dict = Depends(get_current_user),
):
    """PUT /api/sessions/{session_id}/memory/settings —— 临时会话开关。"""
    updated = await chat_store.set_memory_enabled(session_id, user["user_id"], body.memory_enabled)
    _ensure_session(updated)
    return ok(updated, "会话记忆设置已更新")
