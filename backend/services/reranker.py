"""可选 Rerank 重排。

由 RERANK_ENABLED 开关控制；关闭、空候选或调用失败时一律回退向量召回原序，
重排只是增强项，绝不阻断问答主链路。

当前选型：阿里云百炼 gte-rerank-v2（原生 text-rerank HTTP 端点，非 OpenAI 兼容协议），鉴权复用 EMBEDDING_API_KEY（百炼 Key，不单独维护 Rerank Key）；
候选为向量召回 Top-K 片段，按 relevance_score 二次精排，并在结果上附加 rerank_score（不覆盖原向量余弦分 score）。
"""
from __future__ import annotations

import logging

import httpx

from config import settings

logger = logging.getLogger("rag.reranker")


def _hit_text(hit: dict) -> str:
    """取用于重排的片段文本：优先完整 content，回退 snippet。"""
    return (hit.get("content") or hit.get("snippet") or "").strip()


async def maybe_rerank(query: str, hits: list[dict]) -> list[dict]:
    """开关关闭 / 空候选 / 无 Key 时原样返回；开启时按重排分降序并附 rerank_score。

    任何网络、超时、HTTP 非 2xx、返回结构异常都降级为原向量召回顺序，
    保证检索链路不被重排服务拖垮（与项目既有降级哲学一致）。
    """
    if not settings.RERANK_ENABLED or not hits:
        return hits
    if not query or not query.strip():
        return hits
    if not settings.EMBEDDING_API_KEY:
        logger.warning("RERANK_ENABLED=true 但未配置 EMBEDDING_API_KEY，重排回退原序")
        return hits

    documents = [_hit_text(h) or " " for h in hits]
    keep = settings.RERANK_TOP_N if settings.RERANK_TOP_N > 0 else len(hits)
    payload = {
        "model": settings.RERANK_MODEL,
        "input": {"query": query, "documents": documents},
        "parameters": {"top_n": min(keep, len(hits)), "return_documents": False},
    }
    headers = {
        "Authorization": f"Bearer {settings.EMBEDDING_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=settings.RERANK_TIMEOUT) as client:
            resp = await client.post(settings.RERANK_BASE_URL, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        results = (data.get("output") or {}).get("results") or []
        if not results:
            logger.warning("rerank 返回空 results，回退原序；model=%s", settings.RERANK_MODEL)
            return hits

        score_by_index: dict[int, float] = {}
        for item in results:
            idx = item.get("index")
            if isinstance(idx, int) and 0 <= idx < len(hits):
                score_by_index[idx] = float(item.get("relevance_score", 0.0))
        if not score_by_index:
            logger.warning("rerank results 缺少合法 index，回退原序")
            return hits

        ordered = sorted(
            range(len(hits)),
            key=lambda i: score_by_index.get(i, -1.0),
            reverse=True,
        )
        ranked: list[dict] = []
        for i in ordered:
            item = dict(hits[i])
            item["rerank_score"] = round(score_by_index.get(i, 0.0), 4)
            ranked.append(item)
        return ranked[: min(keep, len(ranked))]
    except Exception as exc:  # noqa: BLE001 - 重排为增强项，失败必须降级而非中断主链路
        logger.warning("rerank 调用失败，回退向量召回原序 model=%s：%s", settings.RERANK_MODEL, exc)
        return hits
