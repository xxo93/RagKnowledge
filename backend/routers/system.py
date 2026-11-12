"""系统信息接口 GET /api/system/info

只返回前端展示用的非敏感元信息；严禁返回 API Key、数据库连接串、文件绝对路径。
数据库不可用时 db_ready=False、统计量为 null，不影响接口本身返回。
权限：仅管理员（权限矩阵：查看系统统计与索引信息 = admin）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..config import settings
from ..errors import ok
from ..db import is_ready, extension_version
from ..services import app_config, web_search, biz_query
from ..services.auth_deps import get_current_user, require_admin

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/info")
async def system_info(_: dict = Depends(require_admin)):
    pg_version = await extension_version()
    doc_total = None
    chunk_total = None
    if is_ready():
        try:
            from ..services import vector_store as vs
            # 统计读取失败时置空，不影响系统信息接口响应
            doc_total = await vs.count_stat("total_docs")
            chunk_total = await vs.count_stat("total_chunks")
        except Exception:  # noqa: BLE001
            doc_total = chunk_total = None

    # 检索/生成参数取"当前生效值"（可能已被配置中心覆盖），不要报 .env 里的静态值
    effective = (await app_config.load_snapshot())["groups"]
    eff_map: dict[str, object] = {
        item["key"]: item["current"] for g in effective for item in g["params"]
    }

    return ok({
        "app_stage": "skeleton",
        "llm_provider": settings.LLM_PROVIDER,
        "llm_model": settings.LLM_MODEL,
        "embedding_provider": settings.EMBEDDING_PROVIDER,
        "embedding_model": settings.EMBEDDING_MODEL,
        "embedding_dim": settings.EMBEDDING_DIM,
        "chunk_size": settings.CHUNK_SIZE,
        "chunk_overlap": settings.CHUNK_OVERLAP,
        "top_k": eff_map.get("TOP_K", settings.TOP_K),
        "retrieval_min_score": eff_map.get("RETRIEVAL_MIN_SCORE", settings.RETRIEVAL_MIN_SCORE),
        "retrieval_candidates": eff_map.get("RETRIEVAL_CANDIDATES", settings.RETRIEVAL_CANDIDATES),
        "rerank_enabled": eff_map.get("RERANK_ENABLED", settings.RERANK_ENABLED),
        "temperature": eff_map.get("LLM_TEMPERATURE", settings.LLM_TEMPERATURE),
        "index_type": "hnsw_cosine_ops",
        "max_tool_rounds": settings.MAX_TOOL_ROUNDS,
        "history_rounds": settings.HISTORY_ROUNDS,
        # 上传限制对外暴露，前端据此决定走单请求还是分块上传，避免写死上限；
        # 全部取"当前生效值"，后端改了配置中心，前端刷新页面就跟着变
        "upload_max_mb": settings.UPLOAD_MAX_MB,
        "upload_total_max_mb": eff_map.get("UPLOAD_TOTAL_MAX_MB", settings.UPLOAD_TOTAL_MAX_MB),
        "upload_chunk_max_mb": eff_map.get("UPLOAD_CHUNK_MAX_MB", settings.UPLOAD_CHUNK_MAX_MB),
        "upload_default_chunk_mb": eff_map.get("UPLOAD_DEFAULT_CHUNK_MB", settings.UPLOAD_DEFAULT_CHUNK_MB),
        "upload_default_concurrency": eff_map.get(
            "UPLOAD_DEFAULT_CONCURRENCY", settings.UPLOAD_DEFAULT_CONCURRENCY
        ),
        "upload_chunk_stale_hours": eff_map.get(
            "UPLOAD_CHUNK_STALE_HOURS", settings.UPLOAD_CHUNK_STALE_HOURS
        ),
        "db_ready": is_ready(),
        "pgvector_version": pg_version,
        "doc_total": doc_total,
        "chunk_total": chunk_total,
    })


@router.get("/status")
async def system_status(user: dict = Depends(get_current_user)):
    """GET /api/system/status —— 顶栏"能力状态灯"用的轻量能力开关。

    与 /info 的区别：只返回布尔/短字符串，**不暴露任何统计、模型名与配置项**，
    且登录用户（含 member）即可读取，因为顶栏对所有角色可见。
    """
    await app_config.refresh()
    return ok({
        # 知识库：向量库连接可用即视为可用
        "knowledge_base_ready": is_ready(),
        # 生成能力：配置了 LLM Key 即视为可用（不返回 Key 本身）
        "llm_ready": bool(settings.LLM_API_KEY),
        # 联网兜底：开关打开 + 该供应商配了 Key（百炼用 BAILIAN_SEARCH_API_KEY）；
        # 开关与供应商都取配置中心的当前值，管理员热改后状态灯同步变化
        "web_search_ready": web_search.is_ready(
            await app_config.get_value("WEB_SEARCH_ENABLED"),
            await app_config.get_value("WEB_SEARCH_PROVIDER"),
        ),
        # 业务库查询：注册表可加载 + 有可用 DSN（只读账号未配置时复用主库 DSN，仍走 READ ONLY）
        "biz_query_ready": biz_query.is_ready(),
        # 当前兜底方式，供顶栏/前端提示口径一致（不含任何敏感信息）
        "fallback_mode": str(app_config.get_cached("FALLBACK_MODE")),
        "role": user.get("role"),
    })
