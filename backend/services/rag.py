"""RAG 检索增强编排（F-07 / 架构 §3.2）。

- M2：问句向量化 -> pgvector Top-K 余弦召回 -> 可选 Rerank（默认关）-> 应用层阈值过滤
  -> 规范化来源（doc_name / chunk_index / score / snippet / content）。
- M4：命中片段经 `build_prompt` 交给 `llm.chat_stream` 流式生成；无命中固定话术、不调 LLM；
  LLM 异常降级为确定性来源汇总，绝不编造、不把 5xx 抛给 SSE。
- 连接纪律：`retrieve` 内部 SQL 执行完即归还连接；`stream_answer` 只消费已取回的 hits，
  不在 LLM 流式循环内持有数据库连接（db.py §5.5）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import AsyncIterator

from config import settings
from prompts import NO_RESULT_FALLBACK, LLM_UNAVAILABLE_MSG
from services import embedder as embed_mod, vector_store as vs, reranker

logger = logging.getLogger("rag.rag")


@dataclass
class RagResult:
    answer: str
    sources: list[dict] = field(default_factory=list)
    has_hit: bool = True
    hits: list[dict] = field(default_factory=list)  # M4：完整命中片段（含 content），供流式生成


async def retrieve(query: str, doc_ids: list[int] | None = None) -> list[dict]:
    """向量召回，返回规范化命中片段。

    每项字段：chunk_id / doc_id / doc_name / chunk_index / score(round 4) /
    content(全文，供 build_prompt) / snippet(≤200 字，供来源卡片)。

    - 空问题直接返回 []（不向量化、不查库）；
    - RERANK_ENABLED 开启时经 reranker.maybe_rerank 重排（默认关闭则原样返回）；
    - RETRIEVAL_MIN_SCORE > 0 时应用层按相似度过滤（默认 0.0 不过滤）；
    - 按 (doc_id, chunk_index) 去重，避免重叠切片重复展示（架构 §3.2 片段去重）。
    """
    q = (query or "").strip()
    if not q:
        return []

    emb = embed_mod.get_embedder()
    qvec = emb.embed_query(q)
    hits = await vs.search(qvec, settings.TOP_K, doc_ids=doc_ids)
    hits = await reranker.maybe_rerank(q, hits)

    if settings.RETRIEVAL_MIN_SCORE > 0:
        hits = [h for h in hits if float(h.get("score", 0.0)) >= settings.RETRIEVAL_MIN_SCORE]

    results: list[dict] = []
    seen: set[tuple[int, int]] = set()
    for h in hits:
        key = (int(h["doc_id"]), int(h["chunk_index"]))
        if key in seen:
            continue
        seen.add(key)
        content = h.get("content") or ""
        item = {
            "chunk_id": h.get("chunk_id"),
            "doc_id": h["doc_id"],
            "doc_name": h["doc_name"],
            "chunk_index": h["chunk_index"],
            "score": round(float(h["score"]), 4),
            "content": content,
            "snippet": content[:200],
        }
        # T6：重排开启且命中项带 rerank_score 时一并透出；关闭/降级项无此键，前端隐藏
        if h.get("rerank_score") is not None:
            item["rerank_score"] = h["rerank_score"]
        results.append(item)
    return results


def build_prompt(hits: list[dict], query: str, history: list[dict]) -> list[dict]:
    """拼装 RAG 消息（架构 §3.2 Prompt 结构）：系统约束 + 参考资料 + 历史 + 当前问题。"""
    from prompts import RAG_SYSTEM_PROMPT

    context = "\n\n".join(
        f"[{i + 1}]《{h['doc_name']}》片段 #{h['chunk_index']}（相似度 {h['score']:.2f}）\n{h['content']}"
        for i, h in enumerate(hits)
    )
    return [
        {"role": "system", "content": RAG_SYSTEM_PROMPT},
        {"role": "system", "content": f"【参考资料】\n{context}"},
        *history,
        {"role": "user", "content": query},
    ]


async def answer(query: str, history: list[dict] | None = None) -> RagResult:
    """检索主入口（非流式、供工具层取数）。

    无命中：answer=固定话术、sources=[]、has_hit=False（F-09 不调 LLM、不编造）；
    命中：sources 供来源卡片展示，完整 hits 交给 stream_answer 做流式文本生成，
    本方法不再生成占位答案文本。
    """
    history = history or []
    hits = await retrieve(query)
    if not hits:
        return RagResult(answer=NO_RESULT_FALLBACK, sources=[], has_hit=False, hits=[])

    sources = []
    for h in hits:
        src = {
            "doc_name": h["doc_name"],
            "chunk_index": h["chunk_index"],
            "score": h["score"],
            "snippet": h["snippet"],
        }
        if h.get("rerank_score") is not None:
            src["rerank_score"] = h["rerank_score"]
        sources.append(src)
    return RagResult(answer="", sources=sources, has_hit=True, hits=hits)


async def stream_answer(
    hits: list[dict], query: str, history: list[dict] | None = None
) -> AsyncIterator[str]:
    """命中片段的 LLM 流式答案生成（M4 token 事件来源）。

    - hits 为空：直接产出固定话术（与无命中语义一致，不调用 LLM）；
    - 正常：build_prompt -> llm.chat_stream 逐 token；
    - LLM 不可用/异常：先给不可用提示，再产出确定性来源摘要（仅罗列命中片段，不杜撰结论）。
    """
    if not hits:
        yield NO_RESULT_FALLBACK
        return

    messages = build_prompt(hits, query, history or [])
    try:
        from llm import chat_stream

        async for token in chat_stream(messages):
            yield token
    except Exception as exc:  # noqa: BLE001 - 降级，不击穿 SSE
        logger.warning("LLM 流式生成失败，降级为来源摘要：%s", exc)
        yield LLM_UNAVAILABLE_MSG + "\n"
        for i, h in enumerate(hits[:3]):
            snippet = (h.get("snippet") or "").strip()[:120]
            yield f"\n[参考{i + 1}]《{h['doc_name']}》#{h.get('chunk_index')}：{snippet}"
