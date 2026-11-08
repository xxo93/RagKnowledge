"""文本切片：固定长度 + 重叠窗口（需求 F-01 第 4 步 / 架构 §3.1）。

参数来自配置 CHUNK_SIZE / CHUNK_OVERLAP，禁止在调用点硬编码。
中文按字符切分（BGE 以字/子词为语义单元，无需英文分词）；相邻切片保留 overlap 字符重叠，
缓解跨边界语义断裂。本模块纯函数、可单测。
"""
from __future__ import annotations


def split_text(
    text: str,
    chunk_size: int,
    overlap: int,
) -> list[str]:
    """按滑动窗口切分文本。

    :param text: 已清洗文本
    :param chunk_size: 单块目标字符数
    :param overlap: 相邻块重叠字符数，必须小于 chunk_size
    :return: 非空切片列表（已去除纯空白块）
    """
    if not text or not text.strip():
        return []
    if overlap < 0:
        overlap = 0
    if overlap >= chunk_size:
        # 防御：重叠不允许 >= 块大小，否则窗口无法推进
        overlap = max(0, chunk_size // 5)

    chunks: list[str] = []
    n = len(text)
    step = chunk_size - overlap
    start = 0
    while start < n:
        end = min(start + chunk_size, n)
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        start += step
    return chunks


def chunk_with_index(chunks: list[str]) -> list[dict]:
    """给切片附加文档内序号 chunk_index（入库与溯源展示使用）。"""
    return [
        {"chunk_index": idx, "content": content}
        for idx, content in enumerate(chunks)
    ]
