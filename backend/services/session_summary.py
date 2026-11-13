"""滚动摘要（v3 / WP1-2）。

触发条件：未摘要轮次 > SUMMARY_TRIGGER_ROUNDS；每次把最旧的若干轮并入摘要，
保留最近 SUMMARY_KEEP_ROUNDS 轮不摘要（硬不变量：KEEP ≥ HISTORY_WINDOW_ROUNDS，
保证原文窗口与摘要水位不重叠、不丢最近上下文）。

幂等与并发：写摘要走 `UPDATE ... WHERE summarized_until_record_id < 新水位`，
同一批记录不会被两个并发任务重复推进；先写库成功才算摘要完成。

调用纪律：本模块只在 `done` 帧之后由后台任务调用，失败只记日志，不影响问答。
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

from ..config import settings
from ..prompts import SUMMARY_SYSTEM_PROMPT
from .. import llm
from . import app_config, chat_store, prompt_store

logger = logging.getLogger("rag.session_summary")


def _rule_summary(previous: str, rows: list[dict]) -> str:
    """无 LLM 时的规则摘要：抽取问句与答案首句，拼接后截断。"""
    parts: list[str] = []
    if previous:
        parts.append(previous.strip())
    for row in rows:
        query = (row.get("query") or "").strip()
        answer = (row.get("answer") or "").strip()
        first = re.split(r"[。！？!?\n]", answer, maxsplit=1)[0].strip()
        seg = f"用户问：{query}"
        if first:
            seg += f"；回答要点：{first[:80]}"
        parts.append(seg)
    text = "；".join(p for p in parts if p)
    limit = int(settings.SUMMARY_MAX_CHARS)
    return text[:limit]


async def _llm_summary(previous: str, rows: list[dict], user_id: int) -> str:
    """调用模型压缩；失败或输出为空时回退规则摘要。"""
    if not bool(settings.SUMMARY_LLM_ENABLED):
        return _rule_summary(previous, rows)

    lines: list[str] = []
    for row in rows:
        query = (row.get("query") or "").strip()
        answer = (row.get("answer_digest") or "").strip() or (row.get("answer") or "").strip()
        lines.append(f"用户：{query}\n助手：{answer[:600]}")
    dialog = "\n\n".join(lines)
    limit = int(settings.SUMMARY_MAX_CHARS)
    prompt = await prompt_store.resolve("session_summary", user_id)
    messages = [
        {"role": "system", "content": (prompt.get("content") or SUMMARY_SYSTEM_PROMPT).replace("{max_chars}", str(limit))},
        {
            "role": "user",
            "content": f"【现有摘要】\n{previous or '（无）'}\n\n【新增对话】\n{dialog}",
        },
    ]
    try:
        # N8：轻任务必须有独立超时，小模型/供应商卡住时回退规则摘要，不拖住后台任务。
        text = await asyncio.wait_for(
            asyncio.to_thread(llm.chat, messages, temperature=0.2, purpose="summary"),
            timeout=5.0,
        )
        text = (text or "").strip()
        if text:
            return text[:limit]
    except Exception:  # noqa: BLE001 - 摘要失败不阻塞问答
        logger.warning("滚动摘要 LLM 调用失败，回退规则摘要", exc_info=True)
    return _rule_summary(previous, rows)


async def maybe_summarize(session_id: int, user_id: int) -> Optional[str]:
    """按水位与触发阈值压缩滚动摘要；返回写入成功的新摘要（未触发返回 None）。

    只处理属于该用户且开启记忆的会话；越权/不存在直接返回 None。
    开关与阈值走配置中心，可在线调整、下一条请求生效。
    """
    if not bool(await app_config.get_value("SUMMARY_ENABLED")):
        return None

    state = await chat_store.get_memory_state(int(session_id), int(user_id))
    if state is None or not state.get("memory_enabled", True):
        return None

    cursor = int(state.get("summarized_until_record_id") or 0)
    unsummarized = await chat_store.count_records_after(int(session_id), cursor)
    trigger = max(1, int(await app_config.get_value("SUMMARY_TRIGGER_ROUNDS")))
    if unsummarized <= trigger:
        return None

    window = max(1, int(await app_config.get_value("HISTORY_WINDOW_ROUNDS")))
    keep = max(int(await app_config.get_value("SUMMARY_KEEP_ROUNDS")), window)
    rows = await chat_store.records_between(int(session_id), cursor, limit=60)
    if len(rows) <= keep:
        return None

    to_sum = rows[: len(rows) - keep]
    if not to_sum:
        return None

    new_summary = await _llm_summary(state.get("summary") or "", to_sum, int(user_id))
    until_id = int(to_sum[-1]["record_id"])
    updated = await chat_store.update_summary(
        int(session_id), int(user_id), new_summary, until_id
    )
    if not updated:
        logger.info("摘要水位已被并发推进，跳过本次写入：session=%s until=%s", session_id, until_id)
        return None
    logger.info(
        "滚动摘要完成：session=%s 新增 %d 轮，水位=%s，摘要长度=%d",
        session_id, len(to_sum), until_id, len(new_summary),
    )
    return new_summary


async def summarize_async(session_id: int, user_id: int) -> None:
    """后台任务入口：吞掉所有异常，保证 done 帧之后的记忆维护不影响主链路。"""
    try:
        await maybe_summarize(session_id, user_id)
    except Exception:  # noqa: BLE001
        logger.warning("异步滚动摘要失败（不影响本次问答）：session=%s", session_id, exc_info=True)
