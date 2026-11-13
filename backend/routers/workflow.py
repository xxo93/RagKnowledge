"""工作流 Agent 接口（v3 / WP4 §6.5）。

- POST /api/workflow/plan        生成计划（不执行，默认等待确认）
- POST /api/workflow/run         开始执行
- GET  /api/workflow/{id}/events SSE 实时事件（plan/step_start/step_done/step_error/confirm_required/task_done）
- GET  /api/workflow/{id}        任务详情（时间线）
- POST /api/workflow/{id}/confirm 步骤人工确认
- POST /api/workflow/{id}/cancel  取消（保留已完成结果）
- POST /api/workflow/{id}/resume  恢复中断任务
- GET  /api/workflow/list        当前用户任务列表
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from ..errors import ok
from ..services import io_log, workflow
from ..services.auth_deps import get_current_user

router = APIRouter(prefix="/api/workflow", tags=["workflow"])


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.post("/plan")
async def plan(payload: dict, user: dict = Depends(get_current_user)):
    goal = str(payload.get("goal") or "").strip()
    session_id = payload.get("session_id")
    trace = io_log.get_trace_id()
    task = await workflow.plan_task(
        goal,
        user,
        session_id=int(session_id) if session_id else None,
        trace_id=trace,
    )
    return ok(task)


@router.post("/run")
async def run(payload: dict, user: dict = Depends(get_current_user)):
    task_id = int(payload.get("task_id") or 0)
    if task_id <= 0:
        from ..errors import BizError, ERR_PARAM

        raise BizError(ERR_PARAM, "task_id 必填")
    # 执行环境沿用计划生成时的 trace，保证 planner/executor 的 LLM 日志同 trace
    task = await workflow.get_task(task_id, user)
    io_log.set_trace_id(task.get("trace_id") or io_log.get_trace_id())
    io_log.set_user_id(int(user.get("user_id") or 0))
    task = await workflow.start_task(task_id, user)
    return ok(task)


@router.get("/list")
async def list_tasks(
    status: Optional[str] = None,
    keyword: Optional[str] = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    user: dict = Depends(get_current_user),
):
    total, items = await workflow.list_tasks(user, status=status, keyword=keyword, page=page, size=size)
    return ok({"total": total, "list": items, "page": page, "size": size})


@router.get("/{task_id}")
async def detail(task_id: int, user: dict = Depends(get_current_user)):
    return ok(await workflow.get_task(task_id, user))


@router.get("/{task_id}/events")
async def events(task_id: int, request: Request, user: dict = Depends(get_current_user)):
    task = await workflow.get_task(task_id, user)  # 先做归属校验，再开始流

    async def stream():
        queue = workflow.subscribe(task_id)
        try:
            yield _sse("snapshot", {"task": task, "task_id": task_id})
            for event in workflow.recent_events(task_id):
                yield _sse(event.get("type") or "message", event)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield _sse(event.get("type") or "message", event)
                if event.get("type") == "task_done":
                    break
        finally:
            workflow.unsubscribe(task_id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/{task_id}/confirm")
async def confirm(task_id: int, payload: dict, user: dict = Depends(get_current_user)):
    step_id = int(payload.get("step_id") or 0)
    action = str(payload.get("action") or "continue")
    return ok(await workflow.confirm_task(task_id, step_id, action, user))


@router.post("/{task_id}/cancel")
async def cancel(task_id: int, user: dict = Depends(get_current_user)):
    return ok(await workflow.cancel_task(task_id, user))


@router.post("/{task_id}/pause")
async def pause(task_id: int, user: dict = Depends(get_current_user)):
    return ok(await workflow.pause_task(task_id, user))


@router.post("/{task_id}/resume")
async def resume(task_id: int, user: dict = Depends(get_current_user)):
    task = await workflow.get_task(task_id, user)
    io_log.set_trace_id(task.get("trace_id") or io_log.get_trace_id())
    io_log.set_user_id(int(user.get("user_id") or 0))
    return ok(await workflow.resume_task(task_id, user))
