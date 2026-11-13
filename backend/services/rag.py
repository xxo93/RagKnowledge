"""RAG 检索增强编排。

- 问句向量化 -> pgvector Top-K 余弦召回 -> 可选 Rerank（默认关）-> 应用层阈值过滤 -> 规范化来源（doc_name / chunk_index / score / snippet / content）。
- 命中片段经 `build_prompt` 交给 `llm.chat_stream` 流式生成；无命中固定话术、不调 LLM； LLM 异常降级为确定性来源汇总，绝不编造、不把 5xx 抛给 SSE。
- 连接纪律：`retrieve` 内部 SQL 执行完即归还连接；`stream_answer` 只消费已取回的 hits，不在 LLM 流式循环内持有数据库连接（db.py）。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import AsyncIterator

from ..config import settings
from ..prompts import NO_RESULT_FALLBACK, LLM_UNAVAILABLE_MSG
from . import embedder as embed_mod, vector_store as vs, reranker

logger = logging.getLogger("rag.rag")


def _doc_title(doc_name: str) -> str:
    """由文件名生成展示标题：去扩展名、下划线转空格；失败时回退原文件名。"""
    name = (doc_name or "未命名文档").strip()
    title = re.sub(r"\.[A-Za-z0-9]+$", "", name).replace("_", " ").strip()
    return title or name


def _summary(content: str) -> str:
    """片段摘要：取首句，截断 80 字；无句号时回退前 80 字。"""
    text = (content or "").strip()
    if not text:
        return ""
    first = re.split(r"[。！？!?\n]", text, maxsplit=1)[0].strip()
    return (first or text)[:80]


def _source_fields(h: dict) -> dict:
    """组装来源富字段；doc_id/chunk_id 保留定位能力，旧前端按既有字段兜底。"""
    content = h.get("content") or ""
    excerpt = content[: settings.SOURCE_EXCERPT_CHARS]
    fields = {
        "doc_name": h.get("doc_name"),
        "doc_title": _doc_title(h.get("doc_name") or ""),
        "chunk_id": h.get("chunk_id"),
        "doc_id": h.get("doc_id"),
        "chunk_index": h.get("chunk_index"),
        "doc_summary": h.get("doc_summary") or "",
        "score": round(float(h["score"]), 4),
        "snippet": content[:200],
        "summary": _summary(content),
        "excerpt": excerpt,
        # 来源类型：前端据此区分"内部文档"与"联网结果"，联网项没有 doc_* 字段
        "source_type": "knowledge",
    }
    if h.get("rerank_score") is not None:
        fields["rerank_score"] = h["rerank_score"]
    return fields


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
    - 检索参数全部来自运行时配置中心（候选召回数 / 重排开关与保留数 / 相似度阈值 / Top-K），
      可在管理员配置页在线调整，无需重启；
    - 重排在有候选时按 rerank 分数排序，但**阈值只打在余弦相似度 score 上**：
      rerank 分不是 0–1 校准概率，做绝对过滤会大面积误杀；
    - 过滤后不显式截断到 Top-K 之外的候选不会进入生成与来源卡片；
    - 按 (doc_id, chunk_index) 去重，避免重叠切片重复展示（架构 §3.2 片段去重）。
    """
    q = (query or "").strip()
    if not q:
        return []

    from . import app_config

    candidates = int(await app_config.get_value("RETRIEVAL_CANDIDATES"))
    top_k = int(await app_config.get_value("TOP_K"))
    min_score = float(await app_config.get_value("RETRIEVAL_MIN_SCORE"))
    rerank_enabled = bool(await app_config.get_value("RERANK_ENABLED"))
    rerank_top_n = int(await app_config.get_value("RERANK_TOP_N"))

    emb = embed_mod.get_embedder()
    qvec = emb.embed_query(q)
    hits = await vs.search(qvec, max(candidates, top_k), doc_ids=doc_ids)
    hits = await reranker.maybe_rerank(
        q, hits, enabled=rerank_enabled, top_n=rerank_top_n
    )

    if min_score > 0:
        hits = [h for h in hits if float(h.get("score", 0.0)) >= min_score]

    # 过滤后再截断：先截断会让"候选里排后面但够阈值"的片段被提前丢掉
    hits = hits[:top_k]

    results: list[dict] = []
    seen: set[tuple[int, int]] = set()
    for h in hits:
        key = (int(h["doc_id"]), int(h["chunk_index"]))
        if key in seen:
            continue
        seen.add(key)
        content = h.get("content") or ""
        item = _source_fields(h)
        # 完整 content 仅供 build_prompt 使用，不落进对外来源卡片字段
        item["content"] = content
        results.append(item)
    return results


def build_prompt(
    hits: list[dict],
    query: str,
    history: list[dict],
    extra_system: list[str] | None = None,
    system_prompt: str | None = None,
) -> list[dict]:
    """拼装 RAG 消息（架构 §3.2 Prompt 结构）：

    系统约束 → 会话事实 → 滚动摘要 → 参考资料 → 历史原文 → 当前问题。
    `extra_system` 由 services/memory.build_context 提供（事实块、摘要块），
    放在系统提示之后、参考资料之前，符合 v3 §3.1 的固定注入顺序。
    `system_prompt` 由 prompt_store 解析（WP5-7 版本化/灰度）；为空时回落到代码默认。
    """
    from ..prompts import RAG_SYSTEM_PROMPT

    context = "\n\n".join(
        f"[{i + 1}]《{h['doc_name']}》片段 #{h['chunk_index']}（相似度 {h['score']:.2f}）\n{h['content']}"
        for i, h in enumerate(hits)
    )
    messages: list[dict] = [{"role": "system", "content": system_prompt or RAG_SYSTEM_PROMPT}]
    for block in extra_system or []:
        if block:
            messages.append({"role": "system", "content": block})
    messages.append({"role": "system", "content": f"【参考资料】\n{context}"})
    messages.extend(history)
    messages.append({"role": "user", "content": query})
    return messages


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

    sources = [_source_fields(h) for h in hits]
    return RagResult(answer="", sources=sources, has_hit=True, hits=hits)


async def stream_answer(
    hits: list[dict],
    query: str,
    history: list[dict] | None = None,
    extra_system: list[str] | None = None,
    system_prompt: str | None = None,
) -> AsyncIterator[str]:
    """命中片段的 LLM 流式答案生成（M4 token 事件来源）。

    - hits 为空：直接产出固定话术（与无命中语义一致，不调用 LLM）；
    - 正常：build_prompt -> llm.chat_stream 逐 token；
    - LLM 不可用/异常：先给不可用提示，再产出确定性来源摘要（仅罗列命中片段，不杜撰结论）。
    """
    if not hits:
        yield NO_RESULT_FALLBACK
        return

    messages = build_prompt(hits, query, history or [], extra_system, system_prompt)
    try:
        from ..llm import chat_stream

        async for token in chat_stream(messages, purpose="answer"):
            yield token
    except Exception as exc:  # noqa: BLE001 - 降级，不击穿 SSE
        logger.warning("LLM 流式生成失败，降级为来源摘要：%s", exc)
        yield LLM_UNAVAILABLE_MSG + "\n"
        for i, h in enumerate(hits[:3]):
            snippet = (h.get("snippet") or "").strip()[:120]
            yield f"\n[参考{i + 1}]《{h['doc_name']}》#{h.get('chunk_index')}：{snippet}"


async def stream_general(
    query: str,
    history: list[dict] | None = None,
    temperature: float | None = None,
    system_prompt: str | None = None,
) -> AsyncIterator[str]:
    """通用对话直答（v3 / WP2）：不检索、不注入内部事实/摘要，直接流式生成。

    - 系统提示固定为 `GENERAL_SYSTEM_PROMPT`，明确禁止编造企业内部数据/制度/金额/人名；
    - 保留最近对话原文用于上下文连贯（"再改短一点"这类追问），但不注入会话事实与摘要，
      避免把内部资料带进"未使用内部资料"的回答；
    - LLM 不可用属于本轮失败：抛出 BizError，由 SSE 层发 error 帧并落 status=error 日志；
      其余非致命异常才降级为可读话术。
    """
    from ..prompts import GENERAL_SYSTEM_PROMPT
    from ..errors import BizError, ERR_LLM_UNAVAILABLE

    messages: list[dict] = [{"role": "system", "content": system_prompt or GENERAL_SYSTEM_PROMPT}]
    messages.extend(history or [])
    messages.append({"role": "user", "content": query})
    try:
        from ..llm import chat_stream

        async for token in chat_stream(messages, temperature=temperature, purpose="answer"):
            yield token
    except BizError as exc:
        logger.warning("通用对话生成失败：%s", exc)
        if exc.code == ERR_LLM_UNAVAILABLE:
            raise
        yield LLM_UNAVAILABLE_MSG
    except Exception as exc:  # noqa: BLE001 - 非模型不可用不击穿 SSE
        logger.warning("通用对话生成失败：%s", exc)
        yield LLM_UNAVAILABLE_MSG


def web_sources(items: list[dict]) -> list[dict]:
    """把搜索服务返回项规范成来源卡片字段；联网项没有 doc_id/chunk_index。"""
    sources: list[dict] = []
    for i, item in enumerate(items):
        url = (item.get("url") or "").strip()
        if not url:
            continue
        # 百炼的 search_results 只有 title/url/site_name，没有摘要片段；
        # 这时用站点名兜底，避免卡片出现空白摘要行
        snippet = (item.get("snippet") or "").strip()
        if not snippet and item.get("site"):
            snippet = f"来源站点：{item['site']}"
        sources.append(
            {
                "source_type": "web",
                "doc_name": item.get("site") or item.get("title") or url,
                "doc_title": (item.get("title") or url)[:120],
                "url": url,
                "snippet": snippet[:200],
                "summary": snippet[:80],
                "rank": i + 1,
            }
        )
    return sources


def _build_web_prompt(items: list[dict], query: str, history: list[dict]) -> list[dict]:
    from ..prompts import WEB_ANSWER_SYSTEM_PROMPT

    context = "\n\n".join(
        f"[{i + 1}] {item.get('title') or item.get('url')}\n来源：{item.get('site') or item.get('url')}\n"
        f"摘要：{item.get('snippet') or ''}"
        for i, item in enumerate(items)
    )
    return [
        {"role": "system", "content": WEB_ANSWER_SYSTEM_PROMPT},
        {"role": "system", "content": f"【联网检索结果】\n{context}"},
        *history,
        {"role": "user", "content": query},
    ]


async def stream_web_answer(
    items: list[dict], query: str, history: list[dict] | None = None
) -> AsyncIterator[str]:
    """联网兜底的流式生成：只用检索结果作答；LLM 不可用时退化为结果摘要（仍算 web 来源）。"""
    messages = _build_web_prompt(items, query, history or [])
    try:
        from ..llm import chat_stream

        async for token in chat_stream(messages, purpose="answer"):
            yield token
    except Exception as exc:  # noqa: BLE001 - 不击穿 SSE
        logger.warning("联网兜底答案生成失败，降级为结果摘要：%s", exc)
        yield LLM_UNAVAILABLE_MSG + "\n"
        for i, item in enumerate(items[:3]):
            snippet = (item.get("snippet") or "").strip()[:120]
            yield f"\n[{i + 1}]《{item.get('title') or item.get('url')}》{snippet}"

