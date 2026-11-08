"""系统信息接口 GET /api/system/info（需求 §7.4 / 架构 §6.2）。

只返回前端展示用的非敏感元信息；严禁返回 API Key、数据库连接串、文件绝对路径（§6.2）。
数据库不可用时 db_ready=False、统计量为 null，不影响接口本身返回。
"""
from __future__ import annotations

from fastapi import APIRouter

from config import settings
from errors import ok
from db import db_ready, extension_version

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/info")
async def system_info():
    pg_version = await extension_version()
    doc_total = None
    chunk_total = None
    if db_ready:
        try:
            from services import vector_store as vs
            # 复用统计枚举读取总量（DB 就绪后由 M1 联调；失败静默置 None）
            doc_total = await vs.count_stat("total_docs")
            chunk_total = await vs.count_stat("total_chunks")
        except Exception:  # noqa: BLE001
            doc_total = chunk_total = None

    return ok({
        "app_stage": "skeleton",
        "llm_provider": settings.LLM_PROVIDER,
        "llm_model": settings.LLM_MODEL,
        "embedding_provider": settings.EMBEDDING_PROVIDER,
        "embedding_model": settings.EMBEDDING_MODEL,
        "embedding_dim": settings.EMBEDDING_DIM,
        "chunk_size": settings.CHUNK_SIZE,
        "chunk_overlap": settings.CHUNK_OVERLAP,
        "top_k": settings.TOP_K,
        "rerank_enabled": settings.RERANK_ENABLED,
        "index_type": "hnsw_cosine_ops",
        "max_tool_rounds": settings.MAX_TOOL_ROUNDS,
        "history_rounds": settings.HISTORY_ROUNDS,
        "db_ready": db_ready,
        "pgvector_version": pg_version,
        "doc_total": doc_total,
        "chunk_total": chunk_total,
    })
