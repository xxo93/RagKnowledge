"""回答「依据强度」判定（M8 / WP5-10）。

需求口径：强＝内部命中 ≥2 条；中＝内部命中 1 条；弱＝通用对话/联网检索。
业务库结果属于内部结构化数据，有结果时按强、空结果按中；工具计算结果按中。
纯函数，便于单测；历史记录同样用本函数从 answer_mode + 来源数还原。
"""
from __future__ import annotations

from typing import Optional

STRONG = "strong"
MEDIUM = "medium"
WEAK = "weak"

LABELS = {STRONG: "强", MEDIUM: "中", WEAK: "弱"}


def _mode(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def assess(answer_mode: Optional[str], source_count: int = 0, has_table: bool = False) -> dict:
    """返回 {level, label, reason, source_count, answer_mode}。"""
    mode = _mode(answer_mode)
    count = max(0, int(source_count or 0))

    if mode == "biz_query":
        if has_table:
            return _pack(STRONG, "内部业务库已返回结构化结果", mode, count)
        return _pack(MEDIUM, "内部业务库查询未返回数据", mode, count)

    if mode in ("knowledge", "fallback_guide", "fallback_contact"):
        if count >= 2:
            return _pack(STRONG, f"命中 {count} 条内部资料", mode, count)
        if count == 1:
            return _pack(MEDIUM, "仅命中 1 条内部资料", mode, count)
        return _pack(WEAK, "未命中内部资料，回答来自兜底话术", mode, count)

    if mode == "tool":
        return _pack(MEDIUM, "由内部工具计算得出，非知识库文档", mode, count)

    if mode == "web":
        return _pack(WEAK, "来自联网检索，需自行核实", mode, count)

    if mode in ("general", "chat"):
        return _pack(WEAK, "通用对话，未使用企业内部资料", mode, count)

    if mode.startswith("fallback"):
        return _pack(WEAK, "兜底话术，未使用企业内部资料", mode, count)

    if count >= 2:
        return _pack(STRONG, f"命中 {count} 条内部资料", mode, count)
    if count == 1:
        return _pack(MEDIUM, "仅命中 1 条内部资料", mode, count)
    return _pack(WEAK, "无内部资料依据", mode, count)


def _pack(level: str, reason: str, mode: str, count: int) -> dict:
    return {
        "level": level,
        "label": LABELS[level],
        "reason": reason,
        "source_count": count,
        "answer_mode": mode or None,
    }
