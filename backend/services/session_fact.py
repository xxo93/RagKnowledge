"""会话事实抽取（v3 / WP1-3）。

两路召回：
1. **LLM 结构化抽取**（默认）：每 SESSION_FACT_EVERY_ROUNDS 轮对最近对话抽一次，
   严格 JSON、kind 白名单、confidence ≥ 0.6 才入库；
2. **规则抽取**（LLM 关闭/失败/解析失败时兜底）：只识别“记住…/以后都…/单位是…/待办…”这类
   明确陈述，宁可漏抽也不误抽。

写入语义：同一会话同一 (kind, key) 只有一条 active；新值先作废旧版本，保留历史可审计。
抽取全程异步、失败只记日志，不影响已完成的问答。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Optional

from ..config import settings
from ..prompts import SESSION_FACT_SYSTEM_PROMPT
from .. import llm
from . import app_config, chat_store, prompt_store, session_memory_store

logger = logging.getLogger("rag.session_fact")

KINDS = set(session_memory_store.KINDS)

# 同义 key 归一：保证同一含义跨轮次落到同一 (session_id, kind, key)
_KEY_ALIASES = {
    "金额单位": "金额单位",
    "金额单位是": "金额单位",
    "货币单位": "金额单位",
    "币种": "金额单位",
    "统计口径": "统计口径",
    "时间口径": "统计口径",
    "项目": "项目名",
    "项目名称": "项目名",
    "公司": "公司名",
    "单位": "公司名",
    "客户": "客户名",
}

_PUNCT_RE = re.compile(r"[\s，,。.:：;；!！?？、\"'“”‘’()（）\[\]【】]+")


def normalize_key(raw: str, kind: str = "fact") -> str:
    """把抽取到的 key 归一成稳定短标签：去标点空白、查别名表、截断 64 字符。"""
    key = _PUNCT_RE.sub("", (raw or "").strip())
    if not key:
        return ""
    key = _KEY_ALIASES.get(key, key)
    if len(key) > 64:
        key = key[:64]
    return key


def validate_items(items, *, min_confidence: Optional[float] = None, max_items: Optional[int] = None) -> list[dict]:
    """过滤非法/低置信/重复事实，返回可入库的条目。"""
    min_conf = float(settings.SESSION_FACT_MIN_CONFIDENCE if min_confidence is None else min_confidence)
    max_n = int(settings.SESSION_FACT_MAX_ITEMS if max_items is None else max_items)
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "").strip()
        if kind not in KINDS:
            continue
        key = normalize_key(str(raw.get("key") or ""))
        value = str(raw.get("value") or "").strip()
        if not key or not value:
            continue
        try:
            confidence = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue
        if confidence < min_conf:
            continue
        dedup = (kind, key)
        if dedup in seen:
            continue
        seen.add(dedup)
        out.append({"kind": kind, "key": key, "value": value[:80], "confidence": round(confidence, 2)})
        if len(out) >= max_n:
            break
    return out


_RULES = (
    (re.compile(r"(?:请)?(?:记住|记一下|以后都|今后|从现在(?:起|开始))[：:，,\s]*(.+?)(?:[。！\n]|$)"), "constraint"),
    (re.compile(r"(?:我们|我)(?:的)?(?:单位|公司)(?:是|叫)[：:，,\s]*([^\s。，,]{2,30})"), "entity"),
    (re.compile(r"(?:我们|我)(?:的)?(?:项目|产品|系统)(?:是|叫)[：:，,\s]*([^\s。，,]{2,30})"), "entity"),
    (re.compile(r"(?:待办|TODO|todo)[：:，,\s]*(.+?)(?:[。！\n]|$)"), "todo"),
)


def rule_extract(query: str) -> list[dict]:
    """规则抽取：只处理用户本轮问句里的明确陈述句。"""
    text = (query or "").strip()
    if not text:
        return []
    items: list[dict] = []
    for pattern, kind in _RULES:
        m = pattern.search(text)
        if not m:
            continue
        value = m.group(1).strip()
        if not value:
            continue
        if kind == "constraint":
            key = "约束" + str(len(items) + 1)
        elif kind == "todo":
            key = "待办" + str(len(items) + 1)
        else:
            key = "公司名" if "公司" in text[:10] or "单位" in text[:10] else "项目名"
        items.append({"kind": kind, "key": key, "value": value[:80], "confidence": 0.75})
    return items


def _parse_json(text: str) -> list[dict]:
    """宽容解析模型输出：剥 code fence、取第一个 JSON 对象，失败返回 []。"""
    raw = (text or "").strip()
    if not raw:
        return []
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return []
    items = data.get("items") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


async def _llm_extract(rows: list[dict], user_id: int) -> list[dict]:
    """模型抽取最近对话中的事实；失败/关闭时用规则抽取。"""
    if not bool(settings.SESSION_FACT_LLM_ENABLED):
        return []
    dialog_parts: list[str] = []
    for row in rows:
        q = (row.get("query") or "").strip()
        a = (row.get("answer_digest") or "").strip() or (row.get("answer") or "").strip()
        dialog_parts.append(f"用户：{q}\n助手：{a[:400]}")
    prompt = await prompt_store.resolve("session_fact", user_id)
    messages = [
        {"role": "system", "content": prompt.get("content") or SESSION_FACT_SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(dialog_parts)[: int(settings.SESSION_FACT_MAX_TOKENS) * 2]},
    ]
    text = await asyncio.wait_for(
        asyncio.to_thread(
            llm.chat, messages, temperature=0.0, json_mode=True, purpose="memory_extract"
        ),
        timeout=5.0,
    )
    return _parse_json(text)


async def maybe_extract(session_id: int, user_id: int) -> int:
    """按轮次节奏抽取事实并入库；返回新增/更新条数。

    节奏：会话总轮次每满 SESSION_FACT_EVERY_ROUNDS 触发一次；只抽最近 2 轮，
    避免把一次性查询条件沉淀成“事实”。
    """
    if not bool(await app_config.get_value("SESSION_FACT_ENABLED")):
        return 0

    state = await chat_store.get_memory_state(int(session_id), int(user_id))
    if state is None or not state.get("memory_enabled", True):
        return 0

    total = await chat_store.count_records(int(session_id))
    every = max(1, int(await app_config.get_value("SESSION_FACT_EVERY_ROUNDS")))
    if total == 0 or total % every != 0:
        return 0

    rows = list(reversed(await chat_store.context_records(int(session_id), 2)))
    if not rows:
        return 0

    items: list[dict] = []
    llm_ok = False
    try:
        items = await _llm_extract(rows, int(user_id))
        llm_ok = bool(items)
    except Exception:  # noqa: BLE001 - 模型失败走规则
        logger.warning("会话事实 LLM 抽取失败，回退规则抽取", exc_info=True)

    if not llm_ok:
        for row in rows:
            items.extend(rule_extract(row.get("query") or ""))

    valid = validate_items(
        items, min_confidence=float(await app_config.get_value("SESSION_FACT_MIN_CONFIDENCE"))
    )
    written = 0
    source_id = int(rows[-1]["record_id"]) if rows else None
    for item in valid:
        try:
            saved = await session_memory_store.upsert_fact(
                int(session_id),
                int(user_id),
                item["kind"],
                item["key"],
                item["value"],
                confidence=item["confidence"],
                source_record_id=source_id,
            )
            if saved:
                written += 1
        except Exception:  # noqa: BLE001 - 单条失败不影响其它事实
            logger.warning("写入会话事实失败：key=%s", item.get("key"), exc_info=True)
    if written:
        logger.info("会话事实更新：session=%s 新增/覆盖 %d 条", session_id, written)
    return written


async def extract_async(session_id: int, user_id: int) -> None:
    """后台任务入口：吞掉异常，保证记忆维护不反噬主链路。"""
    try:
        await maybe_extract(session_id, user_id)
    except Exception:  # noqa: BLE001
        logger.warning("异步会话事实抽取失败（不影响本次问答）：session=%s", session_id, exc_info=True)
