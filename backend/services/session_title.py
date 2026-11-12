"""会话首问标题提炼。

MVP 采用规则提炼：去空白/标点/句末语气词，截断到配置长度；稳定性优先，不额外串行调用 LLM。
`SESSION_TITLE_LLM_ENABLED` 为后续 LLM 提炼预留开关，默认关闭；接入 LLM 时仍须以本模块规则结果作为失败兜底。
"""
from __future__ import annotations

import re

from ..config import settings

# 句末疑问/语气词与常见标点；标题不依赖这些字符表达语义
_TRAILING_PARTICLES = "吗呢啊呀嘛吧哦啦嗯？?！!。.，,、；;：: "
_PUNCTUATION = "？?！!。，,、；;：:“”\"'（）()【】[]《》…—-_/\\|~#@%^&*"


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
