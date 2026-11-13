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

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse, Response

from ..errors import (
    BizError,
    ok,
    ERR_SESSION_NOT_FOUND,
    ERR_LLM_UNAVAILABLE,
    ERR_RECORD_NOT_FOUND,
    ERR_PARAM,
    ERR_QUOTA_EXCEEDED,
    ERR_RATE_LIMITED,
)
from ..schemas.chat import ChatStreamRequest, FeedbackCreate
from ..services import chat_store, memory, rag as rag_svc, stream_heartbeat, session_title as session_title_svc
from ..services import session_summary, session_fact
from ..services import app_config, web_search as web_search_svc, internal_terms
from ..services import feedback_store
from ..services import session_memory_store
from ..services import query_rewrite as query_rewrite_svc
from ..services import vector_store as vs
from ..services import io_log
from ..services import quota
from ..services import prompt_store
from ..services import evidence as evidence_svc
from ..services import workflow as workflow_svc
from ..services.auth_deps import get_current_user
from ..agent import graph
from ..agent import decision as decision_mod
from ..agent.tools import search_tool
from ..prompts import (
    NO_RESULT_FALLBACK,
    NOTICE_WEB_USED,
    NOTICE_WEB_UNAVAILABLE,
    LLM_UNAVAILABLE_MSG,
    RAG_SYSTEM_PROMPT,
    GENERAL_SYSTEM_PROMPT,
)
from ..config import settings

logger = logging.getLogger("rag.router.chat")

router = APIRouter(prefix="/api/chat", tags=["chat"])

_SSE_MEDIA = "text/event-stream; charset=utf-8"

# answer_mode 取值：前端据此标注"是否用到内部知识库能力"
MODE_KNOWLEDGE = "knowledge"          # 内部知识库正常作答
MODE_WEB = "web"                      # 联网兜底（明确标注非内部能力）
MODE_GUIDE = "fallback_guide"         # 引导补充描述
MODE_CONTACT = "fallback_contact"     # 提示联系管理员
MODE_TOOL = "tool"                    # 计算器 / 文档统计
MODE_BIZ = "biz_query"                # 业务库查询（结构化表格）
MODE_CHAT = "chat"                    # 寒暄，无工具
MODE_GENERAL = "general"              # v3 / WP2：通用对话直答（未使用企业内部资料）

# 模式被限制 / 能力未开放时的固定话术（确定性、不调模型）
TEXT_MODE_LIMITED = (
    "当前为「仅知识库」模式，只能基于企业内部知识库回答，无法生成代码或做通用创作。"
    "可以点输入框左侧的模式选择器切到「自动」或「仅通用」后重试。"
)
TEXT_WORKFLOW_COMING = (
    "「任务」模式将在后续阶段（M4）开放，当前还不能执行多步骤任务编排。"
    "可以先用「自动」模式提问，我会按知识库 / 业务库 / 通用能力路由。"
)
TEXT_GENERAL_DISABLED = (
    "通用对话能力当前已被管理员关闭，无法回答写代码、写作、通用知识类问题。"
    "请切换到知识库模式咨询企业内部资料相关问题，或联系管理员开启「允许通用对话」。"
)
NOTICE_GENERAL_NO_INTERNAL = (
    "本条回答由通用能力生成，未使用企业内部资料；涉及内部制度、流程、金额或人名的内容请切换到知识库模式查询。"
)



def _frame(event: str, data: dict) -> str:
    """序列化为一帧 SSE 文本；data 中文不转义，前端 JSON.parse 直接消费。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _find_hits(final: dict) -> list[dict]:
    """从最近一次 knowledge_search 观察中取出完整命中片段（含 content，供 build_prompt）。"""
    for obs in reversed(final.get("observations") or []):
        if obs.get("tool") == "knowledge_search":
            return (obs.get("result") or {}).get("hits") or []
    return []


def _find_biz_result(final: dict) -> dict:
    """取最近一次 biz_query 工具结果（含 table / 错误码）。"""
    for obs in reversed(final.get("observations") or []):
        if obs.get("tool") == "biz_query":
            return obs.get("result") or {}
    return {}


def _display_doc_name(name: str) -> str:
    """从文件名派生展示标题；仅去后缀与分隔符，不读取文档正文。"""
    cleaned = re.sub(r"\.[A-Za-z0-9]+$", "", (name or "").strip())
    cleaned = re.sub(r"[_\-]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


async def _fallback_plan(query: str, history: list[dict]) -> dict:
    """内部知识不足时的兜底决策（不含流式生成）。

    返回：answer_mode / notice / sources / text / web_items
    规则（需求文档 §3.2）：
    1. mode=web 且联网可用 → 真联网，answer_mode=web，附"非内部能力"提示条；
    2. 联网失败（未配置/关闭/超时/HTTP 错误/空结果）**无论配置哪种模式**都回落 contact_admin，
       并带 WEB_SEARCH_UNAVAILABLE 提示；
    3. mode=guide / contact_admin → 直接返回可编辑话术，不调模型、不联网。
    """
    mode = str(await app_config.get_value("FALLBACK_MODE"))
    guide_text = str(await app_config.get_value("FALLBACK_TEXT_GUIDE"))
    contact_text = str(await app_config.get_value("FALLBACK_TEXT_CONTACT"))

    if mode == "guide":
        return {"answer_mode": MODE_GUIDE, "notice": None, "sources": [],
                "text": guide_text, "web_items": []}

    if mode != "web":
        return {"answer_mode": MODE_CONTACT, "notice": None, "sources": [],
                "text": contact_text, "web_items": []}

    if not bool(await app_config.get_value("WEB_SEARCH_ENABLED")):
        return {
            "answer_mode": MODE_CONTACT,
            "notice": {"code": "WEB_SEARCH_UNAVAILABLE", "level": "warning",
                       "msg": NOTICE_WEB_UNAVAILABLE.format(reason="管理员关闭了联网检索")},
            "sources": [], "text": contact_text, "web_items": [],
        }

    result = await web_search_svc.ask(query)
    if not result.ok:
        reason = web_search_svc.describe_failure(result.error_code)
        logger.info("联网兜底不可用（%s），回落联系管理员话术", result.error_code)
        return {
            "answer_mode": MODE_CONTACT,
            "notice": {"code": "WEB_SEARCH_UNAVAILABLE", "level": "warning",
                       "msg": NOTICE_WEB_UNAVAILABLE.format(reason=reason)},
            "sources": [], "text": contact_text, "web_items": [],
        }

    return {
        "answer_mode": MODE_WEB,
        "notice": {"code": "WEB_SEARCH_USED", "level": "info", "msg": NOTICE_WEB_USED},
        "sources": rag_svc.web_sources(result.items),
        # 百炼这类"搜索+生成一次完成"的供应商直接给 grounded 答案，不再二次生成；
        # 其他供应商 answer 为空，交给 stream_web_answer 基于片段生成
        "text": result.answer or "",
        "web_items": [] if result.answer else result.items,
    }



async def _post_turn_memory(session_id: int, user_id: int) -> None:
    """done 帧之后的记忆维护：滚动摘要 → 事实抽取。

    两步都吞异常：记忆维护失败只能影响记忆本身，不得回写问答结果或中断 SSE。
    """
    try:
        await session_summary.maybe_summarize(session_id, user_id)
    except Exception:  # noqa: BLE001
        logger.warning("会话滚动摘要失败（不影响问答）：session=%s", session_id, exc_info=True)
    try:
        await session_fact.maybe_extract(session_id, user_id)
    except Exception:  # noqa: BLE001
        logger.warning("会话事实抽取失败（不影响问答）：session=%s", session_id, exc_info=True)


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


@router.post("/feedback")
async def submit_feedback(body: FeedbackCreate, user: dict = Depends(get_current_user)):
    """POST /api/chat/feedback —— 赞/踩 + 原因，自动进入评测集（v3 / WP5-3）。

    只允许对自己会话里的回答反馈；同一回答重复提交按覆盖处理，便于用户改判。
    """
    if body.rating < 0 and not ((body.reason_code or "").strip() or (body.reason_text or "").strip()):
        raise BizError(ERR_PARAM, "请选择或填写「需改进」的原因", http_status=400)
    record = await chat_store.get_record_for_user(body.record_id, user["user_id"])
    if record is None:
        raise BizError(ERR_RECORD_NOT_FOUND, "回答不存在或无权反馈", http_status=404)
    detail = await io_log.get_detail(record["trace_id"]) if record.get("trace_id") else None
    context_usage = (detail or {}).get("context_usage")
    item = await feedback_store.upsert_feedback(
        record_id=record["record_id"],
        session_id=record["session_id"],
        user_id=user["user_id"],
        rating=body.rating,
        reason_code=(body.reason_code or "").strip() or None,
        reason_text=(body.reason_text or "").strip() or None,
        trace_id=record.get("trace_id"),
        query_snapshot=record.get("query"),
        answer_snapshot=record.get("answer"),
        intent=record.get("intent"),
        answer_mode=record.get("answer_mode"),
        sources=record.get("source_ref"),
        context_usage=context_usage,
    )
    return ok(item, "反馈已记录，已进入评测集")


def _client_ip(request: Request) -> Optional[str]:
    """取真实客户端 IP；反向代理场景优先 X-Forwarded-For 首段。"""
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else None


def _export_sources(record: dict) -> list[dict]:
    sources = record.get("source_ref") or []
    if not isinstance(sources, list):
        return []
    out = []
    for s in sources:
        if not isinstance(s, dict):
            continue
        out.append({
            "doc_name": s.get("doc_title") or s.get("doc_name") or "",
            "chunk_index": s.get("chunk_index"),
            "score": s.get("score"),
            "snippet": s.get("snippet") or s.get("excerpt") or "",
        })
    return out


def _export_tools(record: dict) -> list[dict]:
    tools = record.get("tool_call_info") or []
    if not isinstance(tools, list):
        return []
    return [
        {
            "name": t.get("name"),
            "label": t.get("label") or t.get("name"),
            "status": t.get("status"),
            "round": t.get("round"),
        }
        for t in tools if isinstance(t, dict)
    ]


def _render_session_markdown(session: dict, records: list[dict], facts: list[dict], summary: str) -> str:
    lines = [
        f"# {session.get('session_name') or '未命名会话'}",
        "",
        f"- 会话 ID：{session.get('session_id')}",
        f"- 导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 问答轮数：{len(records)}",
        f"- 长期记忆：{'开启' if session.get('memory_enabled', True) else '关闭（临时会话）'}",
        "",
    ]
    if summary:
        lines += ["## 会话摘要", "", summary.strip(), ""]
    if facts:
        lines += ["## 会话事实", ""]
        for f in facts:
            label = f.get("kind") or "事实"
            lines.append(f"- **{label} · {f.get('fact_key') or ''}**：{f.get('fact_value') or ''}")
        lines.append("")
    for i, r in enumerate(records, 1):
        lines += [
            f"## 第 {i} 轮", "",
            f"**问：** {r.get('query') or ''}", "",
            "**答：**", "", (r.get("answer") or "").strip(), "",
        ]
        meta = [
            f"链路：{r.get('answer_mode') or '—'}",
            f"意图：{r.get('intent') or '—'}",
        ]
        ev = r.get("evidence")
        if ev:
            meta.append(f"依据强度：{ev.get('label')}（{ev.get('reason')}）")
        if r.get("trace_id"):
            meta.append(f"trace_id：{r['trace_id']}")
        lines += ["- " + " · ".join(meta), ""]
        sources = _export_sources(r)
        if sources:
            lines += ["### 来源", ""]
            for j, s in enumerate(sources, 1):
                score = f"（相似度 {s['score']:.2f}）" if isinstance(s.get("score"), (int, float)) else ""
                lines.append(f"{j}. 《{s.get('doc_name') or '—'}》片段 #{s.get('chunk_index')} {score}")
            lines.append("")
        tools = _export_tools(r)
        if tools:
            lines += ["### 工具记录", ""]
            for t in tools:
                lines.append(f"- {t.get('label')}（round {t.get('round')}，{t.get('status')}）")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_session_json(session: dict, records: list[dict], facts: list[dict], summary: str) -> str:
    payload = {
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "session": {
            "session_id": session.get("session_id"),
            "session_name": session.get("session_name"),
            "memory_enabled": bool(session.get("memory_enabled", True)),
        },
        "summary": summary or "",
        "facts": facts,
        "records": [
            {
                "record_id": r.get("record_id"),
                "query": r.get("query"),
                "answer": r.get("answer"),
                "trace_id": r.get("trace_id"),
                "intent": r.get("intent"),
                "answer_mode": r.get("answer_mode"),
                "evidence": r.get("evidence"),
                "sources": _export_sources(r),
                "tools": _export_tools(r),
                "create_time": r.get("create_time"),
            }
            for r in records
        ],
    }
    return json.dumps(payload, ensure_ascii=False, default=str, indent=2)


@router.post("/stream")
async def chat_stream(
    body: ChatStreamRequest,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """POST /api/chat/stream —— 流式问答（tool/source/token/done/error 事件）。"""
    trace_id = io_log.get_trace_id()
    io_log.set_user_id(user.get("user_id"))
    ua = request.headers.get("user-agent")
    ip = _client_ip(request)
    started_at = time.perf_counter()
    # 建立流之前：会话不存在或不属于当前用户直接 JSON 错误包络，不进入流式生成。
    # 这类"进 SSE 前的拒绝"同样要落一条 rejected 日志，且 user_id 必须是当前登录用户（验收 §5.7-④）。
    session = await chat_store.get_session_for_user(body.session_id, user["user_id"])
    if session is None:
        io_log.enqueue_turn(
            trace_id=trace_id,
            user_id=user.get("user_id"),
            session_id=body.session_id,
            query=body.query,
            answer=None,
            status="rejected",
            error_code="SESSION_NOT_FOUND",
            error_msg="会话不存在或无权访问",
            elapsed_ms=int((time.perf_counter() - started_at) * 1000),
            ua=ua,
            ip=ip,
        )
        raise BizError(ERR_SESSION_NOT_FOUND, "会话不存在或无权访问，请先新建会话", http_status=404)

    # v3 / WP5-6：进入 SSE 之前做每用户限流与日 token 配额检查。
    # 超限请求统一落一条 rejected 日志（user_id 为当前登录用户），再返回 429 JSON 包络。
    gate = await quota.check_and_consume(int(user["user_id"]))
    if not gate.get("allowed"):
        reason = gate.get("reason")
        error_code = "RATE_LIMITED" if reason == "rate" else "QUOTA_EXCEEDED"
        io_log.enqueue_turn(
            trace_id=trace_id,
            user_id=user.get("user_id"),
            session_id=body.session_id,
            query=body.query,
            answer=None,
            status="rejected",
            error_code=error_code,
            error_msg=gate.get("msg"),
            elapsed_ms=int((time.perf_counter() - started_at) * 1000),
            ua=ua,
            ip=ip,
        )
        code = ERR_RATE_LIMITED if reason == "rate" else ERR_QUOTA_EXCEEDED
        raise BizError(code, gate.get("msg") or "请求被配额策略拦截", http_status=429)

    tool_queue: asyncio.Queue = asyncio.Queue()

    async def _sink(event: dict) -> None:
        await tool_queue.put(event)

    async def _prepare():
        """前置阶段：装配会话记忆（事实+摘要+窗口原文），再执行意图决策与检索。

        独立任务便于静默期发送心跳；记忆读取失败会降级为空上下文，不击穿问答。

        v3 / WP2：先同步决策（显式模式 → 上下文延续 → LLM → 关键词 → 内部事实保护），
        再交给状态图执行工具；模式受限 / 工作流未开放 / 通用能力关闭时直接跳过检索。
        """
        ctx = await memory.build_context(body.session_id, user["user_id"])
        last = await chat_store.get_last_turn(body.session_id)
        terms = await internal_terms.get_terms()
        decision = await asyncio.to_thread(
            decision_mod.decide,
            body.query,
            body.mode,
            (last or {}).get("intent"),
            terms,
        )
        intent = decision.get("intent")
        allow_general = bool(await app_config.get_value("ALLOW_GENERAL_CHAT"))
        # 这几种情况无需检索：仅知识库模式压制了通用问题、任务模式占位、通用能力被关闭
        if (
            decision.get("suppressed_intent")
            or intent == "workflow"
            or (intent == "general" and not allow_general)
        ):
            return ctx, {}, [], decision
        # N9：知识库链路的多轮检索 query 改写。只改 decision.args.query（检索用），
        # body.query 仍用于意图判断、回答生成、SSE 与界面展示。
        rewrite_info: dict = {}
        try:
            rewrite_info = await query_rewrite_svc.rewrite(
                session_id=body.session_id,
                query=body.query,
                history=ctx.history,
                facts=ctx.facts,
                last_intent=(last or {}).get("intent"),
                last_record_id=(last or {}).get("record_id"),
                mode=body.mode,
                intent=decision.get("intent") or "",
                user_id=user.get("user_id"),
            )
        except Exception:  # noqa: BLE001 - 改写失败不得击穿检索
            logger.warning("query 改写异常，沿用原句检索", exc_info=True)
            rewrite_info = {}
        if rewrite_info.get("used"):
            decision = {
                **decision,
                "args": {**(decision.get("args") or {}), "query": rewrite_info["rewritten_query"]},
            }
        if rewrite_info:
            decision["_query_rewrite"] = rewrite_info

        final = await graph.run_once(
            body.query, ctx.history, on_event=_sink, user=user, decision=decision
        )
        sources = final.get("sources") or []
        return ctx, final, sources, decision

    async def _drain_tool_events():
        """投递前置阶段已产生的工具 running/done 帧；队列清空后立即返回。"""
        while not tool_queue.empty():
            event = tool_queue.get_nowait()
            payload = {k: v for k, v in event.items() if k != "type"}
            yield _frame(event.get("type", "tool"), payload)

    # 运行日志元数据：event_gen 只填业务字段，真正落库在包装器的 finally 里，
    # 这样客户端中断 / 生成器被关闭（abort）也能记一条，且 frame_count 统计完整。
    # 必须定义在 event_gen 之外，包装器 finally 才能读到（曾因作用域内定义导致 NameError 断流）。
    started = time.monotonic()
    token_parts: list[str] = []
    meta: dict = {
        "trace_id": trace_id, "user_id": user.get("user_id"), "session_id": body.session_id,
        "query": body.query,
        "record_id": None, "turn_index": None, "intent": None, "answer_mode": None,
        "answer": None, "status": "ok", "error_code": None, "error_msg": None,
        "elapsed_ms": None, "first_token_ms": None, "frame_count": 0,
        "tool_chain": None, "context_usage": None, "ua": ua, "ip": ip,
        "query_rewritten": None, "rewrite_reason": None,
    }

    async def event_gen():
        record_id: int | None = None
        ctx = memory.ContextBundle(usage=memory._usage_empty())
        final, sources, decision = {}, [], {}
        table_for_record = None
        workflow_task_id: int | None = None
        prep: asyncio.Task | None = None
        # WP5-4：检索块 token 与块数，供 done 帧的上下文预算面板使用
        retrieval_tokens = 0
        retrieval_chunks = 0
        # WP5-7：实际生效的提示词版本；写入 done 帧 context_usage 便于排障与验收
        active_prompt_meta: dict = {}

        def _token_frame(payload: dict) -> str:
            if meta["first_token_ms"] is None:
                meta["first_token_ms"] = int((time.monotonic() - started) * 1000)
            return _frame("token", payload)
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
            ctx, final, sources, decision = prep.result()
            async for frame in _drain_tool_events():
                yield frame
            # N9：把本次改写观测送入前端链路抽屉与运行日志；无论是否采用都只在此处发一次
            rewrite_info = decision.get("_query_rewrite") or {}
            if rewrite_info.get("attempted"):
                meta["query_rewritten"] = (
                    rewrite_info.get("rewritten_query") if rewrite_info.get("used") else None
                )
                meta["rewrite_reason"] = rewrite_info.get("reason")
                yield _frame("query_rewrite", {
                    "original_query": rewrite_info.get("original_query"),
                    "rewritten_query": rewrite_info.get("rewritten_query"),
                    "used": bool(rewrite_info.get("used")),
                    "reason": rewrite_info.get("reason"),
                    "elapsed_ms": rewrite_info.get("elapsed_ms"),
                    "model": rewrite_info.get("model"),
                    "cached": bool(rewrite_info.get("cached")),
                })

            answer_mode = MODE_CHAT
            notice: dict | None = None
            table_for_record: dict | None = None
            intent = decision.get("intent") or "knowledge"
            degraded_reason = decision.get("degraded_reason")
            tool = decision.get("tool")
            meta["intent"] = intent

            async def _knowledge_frames(hits: list[dict], kb_sources: list[dict]):
                """知识库路径：命中足够走 RAG，否则按配置兜底；调用方负责收集 token_parts。"""
                nonlocal answer_mode, notice, sources, retrieval_tokens, retrieval_chunks, active_prompt_meta
                min_hits = int(await app_config.get_value("FALLBACK_MIN_HITS"))
                if hits and len(hits) >= min_hits:
                    answer_mode = MODE_KNOWLEDGE
                    sources = kb_sources
                    retrieval_chunks = len(hits)
                    retrieval_tokens = sum(
                        memory.estimate_tokens(h.get("content") or "") for h in hits
                    )
                    active_prompt_meta = await prompt_store.resolve("rag_answer", user["user_id"])
                    if sources:
                        yield _frame("source", {"sources": sources})
                    async for item in stream_heartbeat.aiter_with_heartbeat(
                        rag_svc.stream_answer(
                            hits, body.query, ctx.history, ctx.extra_system(),
                            system_prompt=active_prompt_meta.get("content"),
                        ),
                        settings.SSE_HEARTBEAT_SEC,
                    ):
                        if item is stream_heartbeat.HEARTBEAT_SENTINEL:
                            yield ": ping\n\n"
                            continue
                        token_parts.append(item)
                        yield _token_frame({"text": item})
                    return

                plan = await _fallback_plan(body.query, ctx.history)
                answer_mode = plan["answer_mode"]
                notice = plan["notice"]
                sources = plan["sources"]
                if notice:
                    yield _frame("notice", notice)
                if sources:
                    yield _frame("source", {"sources": sources})
                if plan["web_items"]:
                    async for item in stream_heartbeat.aiter_with_heartbeat(
                        rag_svc.stream_web_answer(plan["web_items"], body.query, ctx.history),
                        settings.SSE_HEARTBEAT_SEC,
                    ):
                        if item is stream_heartbeat.HEARTBEAT_SENTINEL:
                            yield ": ping\n\n"
                            continue
                        token_parts.append(item)
                        yield _token_frame({"text": item})
                else:
                    text = plan["text"] or NO_RESULT_FALLBACK
                    token_parts.append(text)
                    yield _token_frame({"text": text})

            # 3) 按意图/工具选择生成路径；来源帧随分支发送（联网兜底的来源是联网结果）

            if decision.get("suppressed_intent") == "general":
                # 显式「仅知识库」但问题本身需要通用能力：确定性话术 + 切换入口，不检索、不调模型
                answer_mode = MODE_GUIDE
                notice = {"code": "MODE_KNOWLEDGE_ONLY", "level": "info", "msg": TEXT_MODE_LIMITED}
                token_parts.append(TEXT_MODE_LIMITED)
                yield _frame("notice", notice)
                yield _token_frame({"text": TEXT_MODE_LIMITED})
            elif intent == "workflow":
                # v3 / WP4：工作流模式只生成计划（默认不执行），PlanCard 内嵌在本条消息
                answer_mode = "workflow"
                try:
                    task = await workflow_svc.plan_task(
                        body.query, user, session_id=body.session_id, trace_id=trace_id
                    )
                    workflow_task_id = int(task["task_id"])
                    steps = task.get("steps") or []
                    text = (
                        f"已生成 {len(steps)} 步执行计划。"
                        + ("计划已通过校验，点「开始执行」后逐步运行；" if task.get("status") == "awaiting_confirm" else "任务已开始执行；")
                        + "每一步会复用知识库 / 业务库 / 计算器等既有工具，执行过程可暂停或取消。"
                    )
                    meta["workflow_task_id"] = workflow_task_id
                    yield _frame("workflow", {"task": task})
                except BizError as exc:
                    # 计划校验失败是用户可修正的问题，不落 error 帧，按普通回答提示
                    answer_mode = MODE_GUIDE
                    text = exc.msg or "计划生成失败，请调整任务描述后重试。"
                    yield _frame("notice", {"code": "WORKFLOW_PLAN_FAILED", "level": "warning", "msg": text})
                token_parts.append(text)
                yield _token_frame({"text": text})
            elif intent == "general":
                if not bool(await app_config.get_value("ALLOW_GENERAL_CHAT")):
                    # 合规关闭：回落引导话术，不调用模型（验收 §4.5-8）
                    answer_mode = MODE_GUIDE
                    notice = {"code": "GENERAL_CHAT_DISABLED", "level": "warning", "msg": TEXT_GENERAL_DISABLED}
                    token_parts.append(TEXT_GENERAL_DISABLED)
                    yield _frame("notice", notice)
                    yield _token_frame({"text": TEXT_GENERAL_DISABLED})
                else:
                    # 通用直答：不检索、不注入内部事实/摘要、不触发 FALLBACK_MODE
                    answer_mode = MODE_GENERAL
                    notice = {
                        "code": "GENERAL_NO_INTERNAL",
                        "level": "info",
                        "msg": NOTICE_GENERAL_NO_INTERNAL,
                    }
                    yield _frame("notice", notice)
                    temperature = float(await app_config.get_value("GENERAL_TEMPERATURE"))
                    active_prompt_meta = await prompt_store.resolve("general_answer", user["user_id"])
                    async for item in stream_heartbeat.aiter_with_heartbeat(
                        rag_svc.stream_general(
                            body.query, ctx.history, temperature=temperature,
                            system_prompt=active_prompt_meta.get("content"),
                        ),
                        settings.SSE_HEARTBEAT_SEC,
                    ):
                        if item is stream_heartbeat.HEARTBEAT_SENTINEL:
                            yield ": ping\n\n"
                            continue
                        token_parts.append(item)
                        yield _token_frame({"text": item})
            elif tool in ("calculator", "doc_stats"):
                answer_mode = MODE_TOOL
                text = (final.get("answer") or "").strip()
                if text:
                    token_parts.append(text)
                    yield _token_frame({"text": text})
            elif tool == "biz_query":
                biz = _find_biz_result(final)
                if biz.get("ok") and biz.get("table"):
                    table = biz["table"]
                    answer_mode = MODE_BIZ
                    table_for_record = table
                    yield _frame("table", table)
                    summary = f"已按「{table.get('title')}」查询业务库，共 {table.get('total')} 行"
                    if table.get("download_url"):
                        ttl = table.get("download_ttl_minutes") or 0
                        summary += (
                            f"，超过内联上限 {table.get('inline_max')} 行，仅展示前 {len(table.get('rows') or [])} 行；"
                            f"完整结果已脱敏导出为 CSV（{table.get('download_rows')} 行），"
                        )
                        summary += (
                            f"下载链接 {ttl} 分钟内有效，过期前可重复下载" if ttl > 0 else "下载链接长期有效，可重复下载"
                        )
                        if table.get("hard_truncated"):
                            summary += "（结果超过硬上限，超出部分未包含）"
                    elif table.get("truncated"):
                        summary += (
                            f"，超过内联上限 {table.get('inline_max')} 行，"
                            f"本次仅展示前 {len(table.get('rows') or [])} 行；导出暂不可用，请联系管理员"
                        )
                    else:
                        summary += "，敏感字段已在服务端脱敏，明细见下表"
                    summary += "。"
                    token_parts.append(summary)
                    yield _token_frame({"text": summary})
                elif biz.get("code") == "BIZ_NO_INTENT":
                    # 业务库没有对应意图：回落知识库检索，避免"订单流程"这类文档问题被误判
                    result = await search_tool.run(body.query)
                    async for frame in _knowledge_frames(
                        (result or {}).get("hits") or [], (result or {}).get("sources") or []
                    ):
                        yield frame
                else:
                    answer_mode = MODE_CHAT
                    text = (biz.get("message") or "业务库查询未执行，请补充查询对象或联系管理员。").strip()
                    token_parts.append(text)
                    yield _token_frame({"text": text})
            elif tool == "knowledge_search":
                async for frame in _knowledge_frames(
                    _find_hits(final), final.get("sources") or []
                ):
                    yield frame
            else:
                # tool=None（寒暄/无需工具）：确定性引导文案，不触发联网兜底
                text = (final.get("answer") or "").strip() or (
                    "你好，我可以帮你检索知识库、做简单四则运算、统计文档数量，"
                    "或查询业务库（客户/订单/库存/员工考勤/薪资），直接告诉我你的问题即可。"
                )
                token_parts.append(text)
                yield _token_frame({"text": text})

            # 4) 正常走完才落库；若中途断开，CancelledError 在上方 yield 处抛出、跳过本段
            answer_text = "".join(token_parts).strip()
            try:
                record_id = await chat_store.insert_record(
                    body.session_id,
                    body.query,
                    answer_text,
                    tool_call_info=final.get("tool_calls"),
                    source_ref=sources,
                    answer_mode=answer_mode,
                    table_payload=table_for_record,
                    answer_digest=memory.make_digest(answer_text),
                    intent=intent,
                    workflow_task_id=workflow_task_id,
                )
            except Exception:  # noqa: BLE001 - 落库失败不影响本次已流出结果
                logger.warning("对话记录落库失败（不影响本次流式结果）", exc_info=True)
                record_id = None
            saved = record_id is not None
            auto_title: str | None = None
            source_count = len(sources)
            # WP5-10：依据强度。用统一口径判定，done 帧与历史回放共用同一套字段
            evidence = evidence_svc.assess(answer_mode, source_count, has_table=bool(table_for_record))
            meta["record_id"] = record_id
            meta["answer"] = answer_text
            meta["answer_mode"] = answer_mode
            meta["tool_chain"] = final.get("tool_calls")

            # 仅首条记录成功落库时提炼标题；用户已改名、中断、落库失败均不覆盖
            if saved:
                try:
                    record_count = await chat_store.count_records(body.session_id)
                    meta["turn_index"] = record_count
                    if record_count == 1:
                        # N2：开关打开时用 LLM 小模型提炼；失败/超时自动回落规则标题
                        title = await session_title_svc.generate_title(
                            body.query,
                            memory.make_digest(answer_text)[:200],
                            user["user_id"],
                        )
                        if title:
                            auto_title = await chat_store.maybe_set_auto_title(
                                body.session_id, user["user_id"], title
                            )
                except Exception:  # noqa: BLE001 - 标题失败静默降级，不影响回答与落库
                    logger.warning("会话自动标题生成失败（不影响本次回答）", exc_info=True)
                    auto_title = None

            # 5) 记忆维护异步化：摘要压缩 + 事实抽取，done 帧之后独立任务执行，
            #    失败只记日志，绝不阻塞或回写本次回答；临时会话（memory_enabled=false）不抽取
            if saved and ctx.memory_enabled:
                asyncio.create_task(_post_turn_memory(body.session_id, user["user_id"]))

            elapsed_ms = int((time.monotonic() - started) * 1000)
            context_usage = dict(ctx.usage)
            # WP5-4：补齐提示词侧的 token 构成；记忆块（事实/摘要/原文）由 memory 统计。
            system_tokens = 0
            if answer_mode == MODE_KNOWLEDGE:
                system_tokens = memory.estimate_tokens(
                    active_prompt_meta.get("content") or RAG_SYSTEM_PROMPT
                )
            elif answer_mode == MODE_GENERAL:
                system_tokens = memory.estimate_tokens(
                    active_prompt_meta.get("content") or GENERAL_SYSTEM_PROMPT
                )
            query_tokens = memory.estimate_tokens(body.query)
            memory_tokens = int(
                context_usage.get("facts_tokens", 0)
                + context_usage.get("summary_tokens", 0)
                + context_usage.get("history_tokens", 0)
            )
            prompt_total = (
                system_tokens + memory_tokens + int(retrieval_tokens) + query_tokens
            )
            context_usage.update({
                "system_tokens": system_tokens,
                "retrieval_tokens": int(retrieval_tokens),
                "retrieval_chunks": int(retrieval_chunks),
                "query_tokens": query_tokens,
                "memory_tokens": memory_tokens,
                "prompt_tokens_total": prompt_total,
                "budget_utilization_pct": round(
                    memory_tokens / max(1, int(context_usage.get("budget_tokens") or 1)) * 100, 1
                ),
                "answer_chars": len(answer_text),
                "digest_chars": len(memory.make_digest(answer_text)),
                "evidence": evidence,
            })
            if active_prompt_meta.get("version") is not None:
                context_usage.update({
                    "prompt_name": active_prompt_meta.get("name"),
                    "prompt_version": active_prompt_meta.get("version"),
                    "prompt_current_version": active_prompt_meta.get("current_version"),
                    "prompt_gray_percent": active_prompt_meta.get("gray_percent"),
                    "prompt_source": active_prompt_meta.get("source"),
                })
            meta["elapsed_ms"] = elapsed_ms
            meta["context_usage"] = context_usage
            yield _frame("done", {
                "record_id": record_id,
                "saved": saved,
                "session_id": body.session_id,
                "session_title": auto_title,
                "source_count": source_count,
                "answer_mode": answer_mode,
                "evidence_strength": evidence,
                "intent": intent,
                "mode": body.mode,
                "degraded_reason": degraded_reason,
                "elapsed_ms": elapsed_ms,
                "first_token_ms": meta.get("first_token_ms"),
                "trace_id": trace_id,
                "context_usage": context_usage,
                "workflow_task_id": workflow_task_id,
            })
        except asyncio.CancelledError:
            # 客户端中断：取消前置任务、不写库、不补 done；日志由包装器记 status=abort
            if prep and not prep.done():
                prep.cancel()
            logger.info("客户端中断流式问答，本次对话记录不落库 trace=%s", trace_id)
            raise
        except Exception as exc:  # noqa: BLE001 - 致命错误映射为 error 事件，不击穿连接
            if prep and not prep.done():
                prep.cancel()
            code = "LLM_UNAVAILABLE" if getattr(exc, "code", None) == ERR_LLM_UNAVAILABLE else "CHAT_RUNTIME_ERROR"
            meta["status"] = "error"
            meta["error_code"] = code
            meta["error_msg"] = (str(exc) or "回答生成失败")[:512]
            logger.exception("SSE 生成异常 trace=%s", trace_id)
            yield _frame("error", {
                "code": code,
                "msg": "回答生成失败，请稍后重试",
                "trace_id": trace_id,
            })

    async def instrumented_gen():
        """统计帧数 / 首字延迟，并在结束时统一写一条 I/O 运行日志（ok/abort/error）。"""
        try:
            async for frame in event_gen():
                meta["frame_count"] += 1
                yield frame
        except asyncio.CancelledError:
            meta["status"] = "abort"
            raise
        except GeneratorExit:
            # 客户端断开后生成器被关闭：已生成内容仍要保留
            meta["status"] = "abort"
            raise
        finally:
            if meta["status"] == "ok" and not meta.get("answer"):
                # 未走到 done（例如生成器在首个 yield 前被关闭）按中断记
                meta["status"] = "abort"
            if meta["answer"] is None:
                meta["answer"] = "".join(token_parts).strip() or None
            if meta["elapsed_ms"] is None:
                meta["elapsed_ms"] = int((time.monotonic() - started) * 1000)
            try:
                io_log.enqueue_turn(**meta)
            except Exception:  # noqa: BLE001 - 日志失败不影响响应
                pass

    return StreamingResponse(
        instrumented_gen(),
        media_type=_SSE_MEDIA,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/session/{session_id}/export")
async def export_session(
    session_id: int,
    format: str = Query("md", pattern="^(md|json)$"),
    user: dict = Depends(get_current_user),
):
    """GET /api/chat/session/{session_id}/export?format=md|json —— 导出本人会话。

    导出内容含全部问答、来源、工具记录、trace_id 与会话记忆（摘要/事实），
    便于汇报与离线回放；越权与不存在统一 40402。
    """
    session = await chat_store.get_session_for_user(session_id, user["user_id"])
    if session is None:
        raise BizError(ERR_SESSION_NOT_FOUND, "会话不存在或无权访问", http_status=404)
    state = await chat_store.get_memory_state(session_id, user["user_id"])
    summary = (state or {}).get("summary") or ""
    memory_enabled = bool((state or {}).get("memory_enabled", True))
    session_view = {
        "session_id": session_id,
        "session_name": session.get("session_name") or "未命名会话",
        "memory_enabled": memory_enabled,
    }
    facts = []
    if memory_enabled:
        try:
            facts = await session_memory_store.list_facts(session_id, user["user_id"], limit=50)
        except Exception:  # noqa: BLE001 - 记忆读取失败不阻断导出
            logger.warning("导出会话时读取事实失败：session=%s", session_id, exc_info=True)
    _, records = await chat_store.list_records(session_id)
    if format == "json":
        body = _render_session_json(session_view, records, facts, summary)
        media = "application/json; charset=utf-8"
        ext = "json"
    else:
        body = _render_session_markdown(session_view, records, facts, summary)
        media = "text/markdown; charset=utf-8"
        ext = "md"
    filename = f"session-{session_id}-{time.strftime('%Y%m%d')}.{ext}"
    return Response(
        content=body.encode("utf-8"),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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
