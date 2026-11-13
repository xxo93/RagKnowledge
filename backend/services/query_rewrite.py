"""N9：多轮检索 query 改写。

只把短追问 / 代词 / 省略句补成可独立检索的问题，**仅用于知识库检索与 rerank**；
意图判断、回答生成、界面与日志主字段仍使用原句。失败、超时、结果不合法时一律回落原句。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import OrderedDict
from typing import Any

from .. import llm
from ..config import settings
from ..prompts import QUERY_REWRITE_SYSTEM_PROMPT
from . import app_config, prompt_store

logger = logging.getLogger("rag.query_rewrite")

_PRONOUNS = ("这", "那", "它", "他", "她", "该", "上述", "前面", "刚才")
_ELLIPSIS = ("呢", "还有", "另外", "同样", "怎么办", "那…", "那...")
_STOP_CHARS = set("的了是在和与及或就都也还很太呢吗吧啊呀这那他她它们个之其该上述前面刚才还有另外同样怎么办把被给对向从到为以而但却再又更最要能谁几怎会可需应")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,}")
_CACHE: OrderedDict[tuple, dict] = OrderedDict()
_CACHE_LIMIT = 500


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def should_rewrite(query: str, last_intent: str | None, history: list[dict]) -> bool:
    """按需求触发条件判断；无历史首轮、显式模式由调用方先行排除。"""
    q = _norm(query)
    if not q or not history:
        return False
    if len(q) <= 12:
        return True
    if any(token in q for token in _PRONOUNS):
        return True
    if any(token in q for token in _ELLIPSIS):
        return True
    # 上一轮是知识库，且当前句不像独立完整问题：短句、无问号、无明确主语
    if last_intent == "knowledge" and len(q) <= 24 and not _looks_standalone(q):
        return True
    return False


def _looks_standalone(query: str) -> bool:
    """长句且带问号/明确疑问词，视为可独立检索，不承接历史。"""
    q = _norm(query)
    if len(q) <= 12:
        return False
    if any(token in q for token in _PRONOUNS):
        return False
    return q.endswith(("?", "？")) or any(
        token in q for token in ("如何", "怎么", "多少", "哪些", "是否", "为什么", "流程", "标准", "规定")
    )


def _history_text(history: list[dict], limit: int = 4) -> str:
    lines: list[str] = []
    for item in (history or [])[-limit:]:
        role = "用户" if item.get("role") == "user" else "助手"
        content = _norm(item.get("content") or "")
        if content:
            lines.append(f"{role}：{content[:300]}")
    return "\n".join(lines)


def _facts_text(facts: list[dict], limit: int = 8) -> str:
    lines = []
    for item in (facts or [])[:limit]:
        key = _norm(item.get("fact_key") or "")
        value = _norm(item.get("fact_value") or "")
        if value:
            lines.append(f"- {key}：{value}" if key else f"- {value}")
    return "\n".join(lines)


def _key_terms(query: str) -> list[str]:
    """抽取需要保留的原句关键实体：数字、英文词、中文实词片段。"""
    q = _norm(query)
    terms: list[str] = []
    terms.extend(_NUM_RE.findall(q))
    terms.extend(t.lower() for t in _LATIN_RE.findall(q))
    # 按停用字符/虚词切中文片段，保留长度 ≥2 的实词
    buf = []
    for ch in q:
        if ch in _STOP_CHARS or not ("\u4e00" <= ch <= "\u9fff"):
            if len(buf) >= 2:
                terms.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if len(buf) >= 2:
        terms.append("".join(buf))
    # 去重并限制数量，避免过严误杀合理改写
    return list(dict.fromkeys(terms))[:8]


def _term_present(term: str, text: str) -> bool:
    if term in text:
        return True
    # 中文实体允许 4 字以上术语用双字片段校验，避免“住宿上限”被“住宿费上限”误杀
    return any(ch >= "\u4e00" for ch in term) and len(term) >= 4 and any(
        term[i:i + 2] in text for i in range(len(term) - 1)
    )


def _valid(query: str, rewritten: str) -> tuple[bool, str]:
    r = _norm(rewritten)
    if not r:
        return False, "改写结果为空"
    if len(r) > 200:
        return False, "改写结果超过 200 字"
    if r == _norm(query):
        return False, "改写结果与原句相同"
    missing = [t for t in _key_terms(query) if not _term_present(t, r.lower())]
    if missing:
        return False, f"缺少原句关键实体：{','.join(missing[:3])}"
    return True, ""


def _cache_get(key: tuple) -> dict | None:
    item = _CACHE.get(key)
    if item is None:
        return None
    _CACHE.move_to_end(key)
    return item


def _cache_put(key: tuple, value: dict) -> None:
    _CACHE[key] = value
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_LIMIT:
        _CACHE.popitem(last=False)


async def _system_prompt(user_id: int | None) -> str:
    """优先用提示词版本表（可灰度/回滚），存储异常回落到代码默认。"""
    try:
        resolved = await prompt_store.resolve("query_rewrite", user_id)
        content = str(resolved.get("content") or QUERY_REWRITE_SYSTEM_PROMPT)
    except Exception:  # noqa: BLE001 - 提示词故障不影响检索
        content = QUERY_REWRITE_SYSTEM_PROMPT
    # 只为速度：reason 不参与检索，强制模型少生成一个字段，降低首字前阻塞时间
    return content + '\n严格只输出两个字段：{"need_rewrite": bool, "rewritten_query": str}，不要输出 reason。'


def clear_cache() -> None:
    _CACHE.clear()


async def rewrite(
    *,
    session_id: int | None,
    query: str,
    history: list[dict],
    facts: list[dict] | None = None,
    last_intent: str | None = None,
    last_record_id: int | None = None,
    mode: str = "auto",
    intent: str = "knowledge",
    user_id: int | None = None,
) -> dict:
    """返回 {rewritten_query, used, reason, elapsed_ms, model, enabled}。

    不满足触发条件时 used=False，rewritten_query=原句，不调用模型。
    """
    original = _norm(query)
    base = {
        "original_query": original,
        "rewritten_query": original,
        "used": False,
        "reason": "",
        "elapsed_ms": 0,
        "model": "",
        "enabled": True,
        "attempted": False,
    }
    try:
        enabled = bool(await app_config.get_value("QUERY_REWRITE_ENABLED"))
    except Exception:  # noqa: BLE001 - 配置失败按关闭处理，保持旧行为
        enabled = bool(settings.QUERY_REWRITE_ENABLED)
    base["enabled"] = enabled
    if not enabled:
        base["reason"] = "改写开关关闭"
        return base
    if mode not in ("auto", "knowledge"):
        base["reason"] = f"显式模式 {mode} 不参与改写"
        return base
    if intent != "knowledge":
        base["reason"] = f"当前意图 {intent} 不参与改写"
        return base
    if not should_rewrite(original, last_intent, history):
        base["reason"] = "不满足改写触发条件"
        return base

    cache_key = (int(session_id or 0), original, int(last_record_id or 0))
    cached = _cache_get(cache_key)
    if cached:
        return {**cached, "cached": True}

    try:
        timeout_ms = int(await app_config.get_value("QUERY_REWRITE_TIMEOUT_MS"))
    except Exception:  # noqa: BLE001
        timeout_ms = int(settings.QUERY_REWRITE_TIMEOUT_MS)
    timeout_s = max(1.0, min(15.0, timeout_ms / 1000.0))
    facts_text = _facts_text(facts or [])
    user_payload = (
        f"最近对话：\n{_history_text(history) or '（无）'}\n\n"
        f"会话事实：\n{facts_text or '（无）'}\n\n"
        f"当前问题：{original}"
    )
    messages = [
        {"role": "system", "content": await _system_prompt(user_id)},
        {"role": "user", "content": user_payload},
    ]
    started = time.perf_counter()
    base["attempted"] = True
    raw = ""
    model = ""
    try:
        model = llm._model_for("query_rewrite")
        raw = await asyncio.wait_for(
            asyncio.to_thread(
                llm.chat,
                messages,
                temperature=0.0,
                json_mode=True,
                purpose="query_rewrite",
                timeout=timeout_s,
            ),
            timeout=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001 - 超时/模型失败回落原句
        elapsed = int((time.perf_counter() - started) * 1000)
        base["elapsed_ms"] = elapsed
        base["model"] = model
        base["reason"] = f"改写调用失败，回落原句：{type(exc).__name__}"
        logger.warning("query 改写失败，回落原句：%s", type(exc).__name__)
        return base

    elapsed = int((time.perf_counter() - started) * 1000)
    base["elapsed_ms"] = elapsed
    base["model"] = model
    try:
        data = json.loads(raw or "{}")
    except Exception:  # noqa: BLE001
        base["reason"] = "改写结果非 JSON，回落原句"
        return base
    if not isinstance(data, dict) or not bool(data.get("need_rewrite")):
        base["reason"] = str(data.get("reason") or "模型判断无需改写")[:200]
        return base
    rewritten = _norm(str(data.get("rewritten_query") or ""))
    ok, why = _valid(original, rewritten)
    if not ok:
        base["reason"] = why
        return base
    result = {
        **base,
        "rewritten_query": rewritten,
        "used": True,
        "reason": str(data.get("reason") or "承接上一轮追问")[:200],
    }
    _cache_put(cache_key, result)
    return result
