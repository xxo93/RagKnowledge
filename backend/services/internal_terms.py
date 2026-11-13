"""内部实体词表（v3 / WP2）。

来源两处（需求 §4.2）：
1. 配置中心 `INTERNAL_TERMS`（管理员手工维护，逗号分隔）；
2. 知识库成功入库文档的标题派生词（取文件名主干，长度 ≥2，最多 60 个）。

用途：意图被判为 `general` 时做"内部事实优先"保护，命中即强制改走知识库检索。
缓存 300 秒并有并发保护；词表读取失败只降级为配置词表，不影响问答主链路。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from ..config import settings

logger = logging.getLogger("rag.services.internal_terms")

_CACHE_TTL = 300.0
_cache: dict[str, object] = {"terms": [], "ts": 0.0}
_lock = asyncio.Lock()


def _split_terms(raw: str) -> list[str]:
    return [t.strip() for t in re.split(r"[,，;；\n]", raw or "") if t.strip()]


def _title_terms(doc_name: str) -> list[str]:
    """从文档名派生可命中的实体词：去扩展名后的主干（长度 2~20）。"""
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", (doc_name or "").strip())
    stem = re.sub(r"[_\-]+", " ", stem).strip()
    if 2 <= len(stem) <= 20:
        return [stem]
    return []


async def get_terms(force: bool = False) -> list[str]:
    """返回内部实体词表（配置词 + 文档标题词），带 TTL 缓存。"""
    now = time.monotonic()
    if not force and now - float(_cache["ts"] or 0) < _CACHE_TTL and _cache["terms"]:
        return list(_cache["terms"])  # type: ignore[arg-type]

    async with _lock:
        now = time.monotonic()
        if not force and now - float(_cache["ts"] or 0) < _CACHE_TTL and _cache["terms"]:
            return list(_cache["terms"])  # type: ignore[arg-type]

        terms = _split_terms(settings.INTERNAL_TERMS)
        try:
            from . import app_config, vector_store as vs

            raw = await app_config.get_value("INTERNAL_TERMS")
            terms = _split_terms(str(raw)) if raw else terms
            _, docs = await vs.list_documents(1, 60, status=1)
            for doc in docs:
                terms.extend(_title_terms(doc.get("doc_name") or ""))
        except Exception:  # noqa: BLE001 - 词表增强失败不影响问答
            logger.warning("内部实体词表加载失败，仅使用配置词表", exc_info=True)

        deduped: list[str] = []
        for t in terms:
            if t and t not in deduped:
                deduped.append(t)
        _cache["terms"] = deduped[:80]
        _cache["ts"] = time.monotonic()
        return list(deduped[:80])
