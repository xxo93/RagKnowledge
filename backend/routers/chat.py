"""对话路由（需求 §7.3 / F-08 SSE 流式）。

- POST /api/chat/stream：SSE 事件流（tool/token/source/done/error），M3 Agent 化、M4 流式接通；
- GET  /api/chat/record/{session_id}：历史记录回填（M4 接通）。
骨架阶段固定契约；真实 SSE 使用 StreamingResponse + 心跳 + 中断处理（架构 §3.5 / §6.3）。
"""
from __future__ import annotations

from fastapi import APIRouter

from errors import BizError
from schemas.chat import ChatStreamRequest

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post("/stream")
async def chat_stream(body: ChatStreamRequest):
    """POST /api/chat/stream —— 流式问答（M3 接通 Agent、M4 接通 SSE 帧与落库）。

    实现要点（M4）：
    1. 建立流前校验会话存在（不存在直接 JSON 4xx，不进入流）；
    2. memory.load_recent 取最近 5 轮；
    3. agent.graph.run_stream 产出事件，逐帧写成 text/event-stream；
    4. 每 15s 发注释心跳 ": ping"；
    5. finally 感知 is_disconnected / CancelledError，中断不落库（§3.5）。
    """
    raise BizError(50100, "流式问答将在 M3/M4 阶段接通", http_status=501)


@router.get("/record/{session_id}")
async def list_records(session_id: int):
    """GET /api/chat/record/{session_id} —— 历史记录按时间正序回填（M4 接通）。"""
    raise BizError(50100, "历史记录查询将在 M4 阶段接通", http_status=501)
