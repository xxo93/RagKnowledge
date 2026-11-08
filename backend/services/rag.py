"""RAG 检索增强编排（F-07 / 架构 §3.2）。

链路：问句向量化 -> pgvector Top-K 检索 -> 可选 Rerank -> 应用层阈值过滤
      -> Prompt 拼装（系统提示 + 片段 + 历史 + 问题）-> LLM 生成 -> 引用来源。
骨架阶段固定编排契约与数据结构，检索/生成分别在 M2、M4 接通。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from config import settings
from prompts import NO_RESULT_FALLBACK
from services import embedder as embed_mod, vector_store as vs

logger = logging.getLogger("rag.rag")


@dataclass
class RagResult:
    answer: str
    sources: list[dict] = field(default_factory=list)
    has_hit: bool = True


async def retrieve(query: str, doc_ids: list[int] | None = None) -> list[dict]:
    """执行向量检索，返回命中片段（含 doc_name / chunk_index / score / snippet）。"""
    emb = embed_mod.get_embedder()
    qvec = emb.embed_query(query)  # TODO(M2): 接通真实向量化
    hits = await vs.search(qvec, settings.TOP_K, doc_ids=doc_ids)
    # 应用层相似度阈值过滤（默认 0.0 不过滤；不同模型分数分布不同，阈值走配置）
    if settings.RETRIEVAL_MIN_SCORE > 0:
        hits = [h for h in hits if h["score"] >= settings.RETRIEVAL_MIN_SCORE]
    for h in hits:
        h["snippet"] = (h.get("content") or "")[:200]
    return hits


def build_prompt(hits: list[dict], query: str, history: list[dict]) -> list[dict]:
    """拼装 RAG 消息（架构 §3.2 Prompt 结构）。系统提示词来自 prompts.py。"""
    # TODO(M2): 按 [1]《doc_name》片段 #idx（相似度 score）格式拼接片段；历史最近 N 轮
    from prompts import RAG_SYSTEM_PROMPT
    context = "\n\n".join(
        f"[{i+1}]《{h['doc_name']}》片段 #{h['chunk_index']}（相似度 {h['score']:.2f}）\n{h['content']}"
        for i, h in enumerate(hits)
    )
    return [
        {"role": "system", "content": RAG_SYSTEM_PROMPT},
        {"role": "system", "content": f"【参考资料】\n{context}"},
        *history,
        {"role": "user", "content": query},
    ]


async def answer(query: str, history: list[dict] | None = None) -> RagResult:
    """检索 + 生成主入口；无命中直接返回固定话术（F-09 不编造）。"""
    history = history or []
    hits = await retrieve(query)
    if not hits:
        return RagResult(answer=NO_RESULT_FALLBACK, sources=[], has_hit=False)
    # TODO(M2/M4): build_prompt -> llm 流式生成；sources 从 hits 裁剪为来源卡片结构
    sources = [
        {
            "doc_name": h["doc_name"],
            "chunk_index": h["chunk_index"],
            "score": round(h["score"], 4),
            "snippet": h["snippet"],
        }
        for h in hits
    ]
    return RagResult(answer="（M2 阶段接通 LLM 生成）", sources=sources, has_hit=True)
