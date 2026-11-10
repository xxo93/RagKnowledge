"""文本清洗。

职责：去除不可见控制字符、合并多余空白与空行、清理常见页码噪音。
本模块为纯函数、无外部依赖，可直接单测；不做语义改写。
"""
from __future__ import annotations

import re
import unicodedata

# 允许的换行外，其余 C0/C1 控制字符一律剔除（\t \n \r 保留）
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
# 3 个及以上连续换行压成 2 个（段落分隔）
_MULTI_BLANK_RE = re.compile(r"\n{3,}")
# 行内首尾多余空白
_LINE_SPACE_RE = re.compile(r"[ \t]+")
# 疑似页码：单独成行、纯数字或 “第 N 页 / N/N” 的短行
_PAGE_RE = re.compile(r"^\s*(?:第?\s*\d+\s*页?|\d{1,3}\s*/\s*\d{1,3})\s*$")


def clean_text(text: str) -> str:
    """清洗原始抽取文本，返回规整后的纯文本。

    步骤：
    1. Unicode 规范化（NFKC，统一全半角/兼容字符）；
    2. 剔除不可见控制字符；
    3. 统一换行符为 \\n；
    4. 合并多余空行、压缩行内空白；
    5. 剔除疑似独立页码行；
    6. 去除首尾空白。
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = _CONTROL_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    lines: list[str] = []
    for line in text.split("\n"):
        line = _LINE_SPACE_RE.sub(" ", line).strip()
        if _PAGE_RE.match(line):
            continue
        lines.append(line)

    text = "\n".join(lines)
    text = _MULTI_BLANK_RE.sub("\n\n", text)
    return text.strip()
