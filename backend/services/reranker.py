"""可选 Rerank 重排（需求 F-07 第 3 步 / 架构 §2.5，默认关闭）。

由 RERANK_ENABLED 开关控制；关闭时直接使用向量检索 Top-K，本模块不参与链路。
本期不引入真实重排模型（Q6 默认关闭），仅固定接口，后续可替换为 bge-reranker。
"""
from __future__ import annotations

from config import settings


async def maybe_rerank(query: str, hits: list[dict]) -> list[dict]:
    """开关关闭时原样返回；开启时按重排分数重排（M6 前不实现真实模型）。"""
    if not settings.RERANK_ENABLED:
        return hits
    # TODO(M6): 接入 RERANK_MODEL，按 (query, doc) 打分后取 Top-K
    return hits
