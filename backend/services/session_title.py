"""会话首问标题提炼。

规则路径保持稳定：去空白/标点/句末语气词，截断到配置长度。
`SESSION_TITLE_LLM_ENABLED` 打开后，首轮落库后调用小模型生成 8–16 字标题；
超时、报错、空输出、纯符号均回落到规则标题，不阻塞回答主链路。
"""
from __future__ import annotations

import asyncio
import logging
import re

from ..config import settings
from ..prompts import SESSION_TITLE_SYSTEM_PROMPT
from . import app_config, prompt_store
from .. import llm

logger = logging.getLogger("rag.session_title")

# 句末疑问/语气词与常见标点；标题不依赖这些字符表达语义
_TRAILING_PARTICLES = "吗呢啊呀嘛吧哦啦嗯？?！!。.，,、；;：: "
_PUNCTUATION = "？?！!。，,、；;：:“”\"'（）()【】[]《》…—-_/\\|~#@%^&*"
_QUOTES = "“”\"'‘’「」『』《》"
_PREFIXES = ("标题：", "标题:", "会话标题：", "会话标题:")

# 标题是用户可感知的短文本，模型再慢也不能拖住 done 帧；超时后直接用规则标题
_LLM_TIMEOUT_SECONDS = 2.0


def _has_content(text: str) -> bool:
    """标题需包含字母、数字或汉字；纯符号/空白不生成标题。"""
    return bool(re.search(r"[\w\u4e00-\u9fff]", text))


def derive_title(query: str) -> str:
    """按首问提炼短标题；无可提炼内容时返回空串，调用方保持默认会话名。"""
    text = (query or "").strip()
    if not text:
        return ""

    # 合并换行与连续空白；中文标题不保留空格干扰截断
    text = re.sub(r"\s+", "", text)
    text = "".join(ch for ch in text if ch not in _PUNCTUATION)
    text = text.rstrip(_TRAILING_PARTICLES)

    if not _has_content(text):
        return ""

    max_len = max(1, settings.SESSION_TITLE_MAX_LEN)
    if len(text) <= max_len:
        return text

    clipped = text[:max_len].rstrip(_TRAILING_PARTICLES)
    return f"{clipped}…"


def sanitize_title(raw: str, max_len: int | None = None) -> str:
    """规范化模型输出：取首行、去前缀/引号/句末标点、限长。

    返回空串表示不可用，由调用方回落到规则标题。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    # 模型偶尔会输出“标题：xxx”或多行解释，只取第一行有效内容
    text = text.splitlines()[0].strip()
    for prefix in _PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    text = text.strip(_QUOTES).strip()
    # 模型可能在中英混排时补空格；标题统一去空白，避免与规则标题风格不一致
    text = re.sub(r"\s+", "", text)
    text = "".join(ch for ch in text if ch not in _PUNCTUATION).strip()
    text = text.rstrip(_TRAILING_PARTICLES)
    if not _has_content(text):
        return ""
    limit = max(1, int(max_len or settings.SESSION_TITLE_MAX_LEN))
    if len(text) > limit:
        text = text[:limit].rstrip(_TRAILING_PARTICLES)
    return text


async def generate_title(query: str, answer_digest: str, user_id: int) -> str:
    """首轮落库后生成标题：开关关闭或失败时返回规则标题。"""
    rule = derive_title(query)
    try:
        enabled = bool(await app_config.get_value("SESSION_TITLE_LLM_ENABLED"))
    except Exception:  # noqa: BLE001 - 配置不可读时按默认关闭处理
        enabled = bool(settings.SESSION_TITLE_LLM_ENABLED)
    if not enabled or not rule:
        return rule

    try:
        prompt = await prompt_store.resolve("session_title", int(user_id))
        system = prompt.get("content") or SESSION_TITLE_SYSTEM_PROMPT
        digest = (answer_digest or "").strip()[:200]
        user_content = f"【首问】\n{query.strip()}\n\n【回答摘要】\n{digest or '（无）'}"
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]
        text = await asyncio.wait_for(
            asyncio.to_thread(
                llm.chat,
                messages,
                temperature=0.2,
                purpose="title",
                timeout=_LLM_TIMEOUT_SECONDS - 0.1,
            ),
            timeout=_LLM_TIMEOUT_SECONDS,
        )
        cleaned = sanitize_title(text)
        if cleaned:
            return cleaned
        logger.info("会话标题模型输出为空或不可用，回落规则标题：session_user=%s", user_id)
    except Exception:  # noqa: BLE001 - 标题失败不影响回答
        logger.warning("会话标题 LLM 调用失败，回落规则标题", exc_info=True)
    return rule
