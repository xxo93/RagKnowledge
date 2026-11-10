"""工具调用防重复键（架构 §4.2 assess 再规划保护）。

多轮再规划若允许回到工具节点，必须避免对同一 (tool, args) 重复调用，防止死循环与重复计费；
本模块提供规范化、稳定、可哈希的调用键与重复判定纯函数（离线、无外部依赖、可单测）。

当前运行仍为单轮收敛（`route_after_assess` 直接收敛到 answer），本模块先把防重复契约与
节点保护就位；未来开启多轮再规划时，直接以 `is_duplicate` 拦截重复调用即可。
"""
from __future__ import annotations

import json
import re

_WS = re.compile(r"\s+")


def _collapse_space(value: object) -> str:
    """折叠连续空白为单个空格并去首尾（保留运算符等位置的单空格，用于表达式）。"""
    return _WS.sub(" ", str(value or "")).strip()


def _strip_all_space(value: object) -> str:
    """删除全部空白（中文/检索问句中空白无语义，避免等价问句产生不同键）。"""
    return _WS.sub("", str(value or ""))


def normalize_args(tool: str, args: dict | None) -> dict:
    """按工具语义规范化入参，使等价调用产生同一稳定键；忽略空白差异。"""
    if not isinstance(args, dict):
        return {}
    if tool == "knowledge_search":
        return {"query": _strip_all_space(args.get("query"))}
    if tool == "calculator":
        return {"expression": _collapse_space(args.get("expression"))}
    if tool == "doc_stats":
        return {"stat_type": str(args.get("stat_type") or "")}
    # 未知工具：按键名排序后转字符串，保证键序不影响等价性
    return {k: str(v) for k, v in sorted(args.items())}


def call_key(tool: str, args: dict | None) -> str:
    """返回稳定可哈希的调用键：`tool|规范化参数 JSON`（排序键、不转义中文）。"""
    norm = normalize_args(tool, args)
    return f"{tool}|{json.dumps(norm, sort_keys=True, ensure_ascii=False)}"


def is_duplicate(seen: list[str] | set[str] | None, tool: str, args: dict | None) -> bool:
    """判定本次调用是否与历史调用键重复。"""
    return call_key(tool, args) in set(seen or [])
