"""Embedding 适配器（架构 ADR-008：协议 + 工厂，选型可替换）。

契约 Embedder：
- dim: 向量维度，必须与配置 EMBEDDING_DIM 及表列 vector(N) 一致（§5.4 三维约束）；
- embed_documents(texts): 入库批量向量化，返回顺序与输入严格对齐（ingest 按 zip 对齐）；
- embed_query(text): 检索问句向量化（与入库同模型）。

实现分支（EMBEDDING_PROVIDER 驱动）：
- api:    OpenAI 兼容 Embedding HTTP；当前选型 = 阿里云百炼 text-embedding-v3
          （默认 1024 维，与 document_chunk.embedding vector(1024) 匹配；兼容模式单次最多 10 行，
          本类按 min(配置批次, 10) 分批，显式传 dimensions 锁定维度）；
- local:  sentence-transformers + BGE（需 torch，后续可选分支，当前占位）；
- ollama: 本地 Ollama 服务（后续可选分支，当前占位）。

业务层只允许经 get_embedder() 获取实现（依赖规则 R6：外部 Embedding 只走本模块）。
"""
from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from config import settings
from errors import BizError, ERR_INGEST_FAILED

logger = logging.getLogger("rag.embedder")

# 百炼 text-embedding-v3 OpenAI 兼容模式：单次请求最大行数（超出必须自行分批）
_API_MAX_ROWS = 10

_api_client = None


@runtime_checkable
class Embedder(Protocol):
    dim: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def _assert_dim(vectors: list[list[float]]) -> None:
    """校验向量维度；发现与配置不符时直接报错，避免写入脏向量数据。"""
    if not vectors:
        return
    expected = settings.EMBEDDING_DIM
    for vec in vectors:
        if len(vec) != expected:
            raise BizError(
                ERR_INGEST_FAILED,
                f"向量维度不符：期望 {expected}，实际 {len(vec)}；换模型需同步修改 EMBEDDING_DIM 并重建列",
                http_status=500,
            )


def _get_api_client():
    """惰性创建 OpenAI 兼容 Embedding 客户端（与 llm.py 同风格，不在应用启动期联网建连）。"""
    global _api_client
    if _api_client is not None:
        return _api_client
    if not settings.EMBEDDING_API_BASE_URL:
        raise BizError(ERR_INGEST_FAILED, "未配置 EMBEDDING_API_BASE_URL", http_status=500)
    if not settings.EMBEDDING_API_KEY:
        raise BizError(ERR_INGEST_FAILED, "未配置 EMBEDDING_API_KEY，Embedding 服务不可用", http_status=500)
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise BizError(
            ERR_INGEST_FAILED, "缺少 openai 依赖，请先安装 requirements.txt", http_status=500
        ) from exc

    _api_client = OpenAI(
        api_key=settings.EMBEDDING_API_KEY,
        base_url=settings.EMBEDDING_API_BASE_URL,
        timeout=settings.LLM_TIMEOUT,
    )
    return _api_client


def _call_embeddings(texts: list[str]) -> list[list[float]]:
    """调用一次兼容 /embeddings；按返回 index 归位，保证输出与输入顺序一致。"""
    client = _get_api_client()
    resp = client.embeddings.create(
        model=settings.EMBEDDING_MODEL,
        input=texts,
        dimensions=settings.EMBEDDING_DIM,
        encoding_format="float",
    )
    ordered: dict[int, list[float]] = {}
    for item in resp.data:
        ordered[item.index] = [float(x) for x in item.embedding]
    missing = [i for i in range(len(texts)) if i not in ordered]
    if missing:
        raise BizError(
            ERR_INGEST_FAILED, f"Embedding 返回缺少向量下标：{missing[:5]}", http_status=500
        )
    return [ordered[i] for i in range(len(texts))]


def _api_batch_size() -> int:
    """实际单批行数：不超过供应商上限 10，且不小于 1。"""
    size = min(settings.EMBEDDING_BATCH_SIZE, _API_MAX_ROWS)
    return size if size > 0 else _API_MAX_ROWS


class ApiEmbedder:
    """OpenAI 兼容 Embedding（当前选型：百炼 text-embedding-v3，默认 1024 维）。"""

    dim = settings.EMBEDDING_DIM

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        batch = _api_batch_size()
        vectors: list[list[float]] = []
        for start in range(0, len(texts), batch):
            part = texts[start:start + batch]
            try:
                vectors.extend(_call_embeddings(part))
            except BizError:
                raise
            except Exception as exc:  # noqa: BLE001 - 统一映射为入库失败业务码
                logger.warning("Embedding 批次调用失败 model=%s：%s", settings.EMBEDDING_MODEL, exc)
                raise BizError(
                    ERR_INGEST_FAILED, "向量化服务调用失败，请稍后重试", http_status=500
                ) from exc
        _assert_dim(vectors)
        return vectors

    def embed_query(self, text: str) -> list[float]:
        if not text or not text.strip():
            raise BizError(ERR_INGEST_FAILED, "检索问句为空，无法向量化", http_status=400)
        try:
            vec = _call_embeddings([text])[0]
        except BizError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("检索向量化失败 model=%s：%s", settings.EMBEDDING_MODEL, exc)
            raise BizError(
                ERR_INGEST_FAILED, "检索向量化失败，请稍后重试", http_status=500
            ) from exc
        _assert_dim([vec])
        return vec


def get_embedder() -> Embedder:
    """按 EMBEDDING_PROVIDER 返回具体实现（工厂模式）。"""
    provider = settings.EMBEDDING_PROVIDER.lower()
    if provider == "api":
        return ApiEmbedder()
    if provider == "local":
        return LocalBGEEmbedder()
    if provider == "ollama":
        return OllamaEmbedder()
    raise BizError(ERR_INGEST_FAILED, f"未知 EMBEDDING_PROVIDER：{provider}", http_status=500)


class LocalBGEEmbedder:
    """本地 sentence-transformers BGE 实现（未启用；当前选型为 api/text-embedding-v3）。"""

    dim = settings.EMBEDDING_DIM

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("本地 BGE 分支未启用（当前选型为 api / text-embedding-v3）")

    def embed_query(self, text: str) -> list[float]:
        raise NotImplementedError("本地 BGE 分支未启用")


class OllamaEmbedder:
    """Ollama 本地服务实现（未启用；当前选型为 api/text-embedding-v3）。"""

    dim = settings.EMBEDDING_DIM

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("Ollama 分支未启用（当前选型为 api / text-embedding-v3）")

    def embed_query(self, text: str) -> list[float]:
        raise NotImplementedError("Ollama 分支未启用")
