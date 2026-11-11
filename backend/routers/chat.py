"""对话问答路由（SSE 流式）。

帧协议对齐前端 `api/sse.js`：`event: <name>\ndata: <json>\n\n`，空行分隔；`tool/token/source/done/error`。
编排纪律：
1. 流前校验会话存在（不存在直接 JSON 40402，不进入 SSE 流）；
2. memory.load_recent 取最近 HISTORY_ROUNDS 轮历史，经 build_prompt 参与 LLM 上下文；
3. 先由 agent.graph.run_once 完成工具调度与观察（SQL 连接在此阶段归还），再按工具类型产出 token；
4. calculator/doc_stats 输出确定性结果文本；knowledge_search 命中走 rag.stream_answer 流式 LLM，无命中走固定话术（不调 LLM、不编造）；
5. 仅在 token 完整产出、流程正常结束时 insert_record；客户端中断（CancelledError）不落库；
6. 不在 LLM 流式循环内持有数据库连接（db.py §5.5）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from errors import BizError, ok, ERR_SESSION_NOT_FOUND
from schemas.chat import ChatStreamRequest
from services import chat_store, memory, rag as rag_svc, stream_heartbeat, session_title as session_title_svc
from services import vector_store as vs
from services.auth_deps import get_current_user
from agent import graph
from prompts import NO_RESULT_FALLBACK
from config import settings

logger = logging.getLogger("rag.router.chat")

router = APIRouter(prefix="/api/chat", tags=["chat"])

_SSE_MEDIA = "text/event-stream; charset=utf-8"


def _frame(event: str, data: dict) -> str:
    """序列化为一帧 SSE 文本；data 中文不转义，前端 JSON.parse 直接消费。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _find_hits(final: dict) -> list[dict]:
    """从最近一次 knowledge_search 观察中取出完整命中片段（含 content，供 build_prompt）。"""
    for obs in reversed(final.get("observations") or []):
        if obs.get("tool") == "knowledge_search":
            return (obs.get("result") or {}).get("hits") or []
    return []


def _display_doc_name(name: str) -> str:
    """从文件名派生展示标题；仅去后缀与分隔符，不读取文档正文。"""
    cleaned = re.sub(r"\.[A-Za-z0-9]+$", "", (name or "").strip())
    cleaned = re.sub(r"[_\-]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


@router.get("/suggestions")
async def list_suggestions(user: dict = Depends(get_current_user)):
    """GET /api/chat/suggestions —— 登录用户只读示例问题；基于成功入库文档标题轻量派生，不调用 LLM。"""
    _, docs = await vs.list_documents(1, 6, status=1)
    doc_questions: list[str] = []
    for doc in docs:
        title = _display_doc_name(doc.get("doc_name") or "")
        if title:
            doc_questions.append(f"「{title}」里有哪些关键内容？")
    fixed_questions = [
        "1234 × 56 等于多少？",
        "知识库现在一共有多少份文档？",
    ]
    questions = list(dict.fromkeys(doc_questions[:2] + fixed_questions))
    return ok({"questions": questions[:4]})


@router.post("/stream")
async def chat_stream(body: ChatStreamRequest, user: dict = Depends(get_current_user)):
    """POST /api/chat/stream —— 流式问答（tool/source/token/done/error 事件）。"""
    # 建立流之前：会话不存在或不属于当前用户直接 JSON 错误包络，不进入流式生成
    session = await chat_store.get_session_for_user(body.session_id, user["user_id"])
    if session is None:
        raise BizError(ERR_SESSION_NOT_FOUND, "会话不存在或无权访问，请先新建会话", http_status=404)

    tool_queue: asyncio.Queue = asyncio.Queue()

    async def _sink(event: dict) -> None:
        await tool_queue.put(event)

    async def _prepare():
        """前置阶段：加载历史并执行 Agent 决策、检索与重排；独立任务便于静默期发送心跳。"""
        history = await memory.load_recent(body.session_id)
        final = await graph.run_once(body.query, history, on_event=_sink)
        decision = (final.get("decision") or {}).get("tool")
        sources = final.get("sources") or []
        return history, final, sources, decision

    async def _drain_tool_events():
        """投递前置阶段已产生的工具 running/done 帧；队列清空后立即返回。"""
        while not tool_queue.empty():
            event = tool_queue.get_nowait()
            payload = {k: v for k, v in event.items() if k != "type"}
            yield _frame(event.get("type", "tool"), payload)

    async def event_gen():
        started = time.monotonic()
        token_parts: list[str] = []
        record_id: int | None = None
        history, final, sources, decision = [], {}, [], None
        prep: asyncio.Task | None = None
        try:
            prep = asyncio.create_task(_prepare())
            # 前置阶段长时间无业务帧时发送 SSE 注释帧，前端会跳过非业务行
            while not prep.done():
                try:
                    await asyncio.wait_for(asyncio.shield(prep), timeout=settings.SSE_HEARTBEAT_SEC)
                except asyncio.TimeoutError:
                    async for frame in _drain_tool_events():
                        yield frame
                    yield ": ping\n\n"
            history, final, sources, decision = prep.result()
            async for frame in _drain_tool_events():
                yield frame

            # 来源事件先于答案文本发送，前端可先渲染来源卡片
            if sources:
                yield _frame("source", {"sources": sources})

            # 3) token 事件：按决策工具选择生成路径
            if decision in ("calculator", "doc_stats"):
                text = (final.get("answer") or "").strip()
                if text:
                    token_parts.append(text)
                    yield _frame("token", {"text": text})
            elif decision == "knowledge_search":
                hits = _find_hits(final)
                if sources and hits:
                    # token 静默期发送心跳；底层生成器不因本次超时等待被取消
                    async for item in stream_heartbeat.aiter_with_heartbeat(
                        rag_svc.stream_answer(hits, body.query, history),
                        settings.SSE_HEARTBEAT_SEC,
                    ):
                        if item is stream_heartbeat.HEARTBEAT_SENTINEL:
                            yield ": ping\n\n"
                            continue
                        token_parts.append(item)
                        yield _frame("token", {"text": item})
                else:
                    token_parts.append(NO_RESULT_FALLBACK)
                    yield _frame("token", {"text": NO_RESULT_FALLBACK})
            else:
                # tool=None（寒暄/无需工具）：确定性引导文案，不暴露内部实现细节
                text = (final.get("answer") or "").strip() or (
                    "你好，我可以帮你检索知识库、做简单四则运算，或统计文档数量，直接告诉我你的问题即可。"
                )
                token_parts.append(text)
                yield _frame("token", {"text": text})

            # 4) 正常走完才落库；若中途断开，CancelledError 在上方 yield 处抛出、跳过本段
            answer_text = "".join(token_parts).strip()
            try:
                record_id = await chat_store.insert_record(
                    body.session_id,
                    body.query,
                    answer_text,
                    tool_call_info=final.get("tool_calls"),
                    source_ref=sources,
                )
            except Exception:  # noqa: BLE001 - 落库失败不影响本次已流出结果
                logger.warning("对话记录落库失败（不影响本次流式结果）", exc_info=True)
                record_id = None
            saved = record_id is not None
            auto_title: str | None = None
            source_count = len(sources)

            # 仅首条记录成功落库时提炼标题；用户已改名、中断、落库失败均不覆盖
            if saved:
                try:
                    record_count = await chat_store.count_records(body.session_id)
                    if record_count == 1:
                        title = session_title_svc.derive_title(body.query)
                        if title:
                            auto_title = await chat_store.maybe_set_auto_title(
                                body.session_id, user["user_id"], title
                            )
                except Exception:  # noqa: BLE001 - 标题失败静默降级，不影响回答与落库
                    logger.warning("会话自动标题生成失败（不影响本次回答）", exc_info=True)
                    auto_title = None

            elapsed_ms = int((time.monotonic() - started) * 1000)
            yield _frame("done", {
                "record_id": record_id,
                "saved": saved,
                "session_id": body.session_id,
                "session_title": auto_title,
                "source_count": source_count,
                "elapsed_ms": elapsed_ms,
            })
        except asyncio.CancelledError:
            # 客户端中断：取消前置任务、不写库、不补 done
            if prep and not prep.done():
                prep.cancel()
            logger.info("客户端中断流式问答，本次对话记录不落库")
            raise
        except Exception as exc:  # noqa: BLE001 - 致命错误映射为 error 事件，不击穿连接
            if prep and not prep.done():
                prep.cancel()
            logger.exception("SSE 生成异常")
            yield _frame("error", {"code": "CHAT_RUNTIME_ERROR", "msg": "回答生成失败，请稍后重试"})

    return StreamingResponse(
        event_gen(),
        media_type=_SSE_MEDIA,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/record/{session_id}")
async def list_records(
    session_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: Optional[int] = Query(None, ge=0),
    user: dict = Depends(get_current_user),
):
    """GET /api/chat/record/{session_id}?limit=&offset= —— 最近历史按时间正序回填。"""
    session = await chat_store.get_session_for_user(session_id, user["user_id"])
    if session is None:
        raise BizError(ERR_SESSION_NOT_FOUND, "会话不存在或无权访问", http_status=404)
    total, records = await chat_store.list_records(session_id, limit=limit, offset=offset)
    return ok({"total": total, "list": records, "limit": limit, "offset": offset})
