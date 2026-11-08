"""Embedding 适配器（架构 ADR-008：协议 + 工厂，选型可替换）。

契约 Embedder：
- dim: 向量维度，必须与配置 EMBEDDING_DIM 及表列 vector(N) 一致（§5.4 三维约束）；
- embed_documents(texts): 入库批量向量化；
- embed_query(text): 检索问句向量化（与入库同模型，必要时加查询前缀）。

实现分支：
- local: sentence-transformers + BGE（需 torch，体积大，M1 前定版 Q1）；
- api:   OpenAI 兼容 Embedding HTTP 接口；
- ollama: 本地 Ollama 服务。
骨架阶段：仅固定协议与工厂入口，真实实现按选定 Provider 在 M1 补齐。
"""
from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from config import settings
from errors import BizError, ERR_INGEST_FAILED

logger = logging.getLogger("rag.embedder")


@runtime_checkable
class Embedder(Protocol):
    dim: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def _assert_dim(vectors: list[list[float]]) -> None:
    """运行时维度自检（§5.4）：任一向量维度不符即失败，避免脏数据写入。"""
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


class ApiEmbedder:
    """OpenAI 兼容 Embedding API 实现（M1 补齐 HTTP 调用与批量分批）。"""

    dim = settings.EMBEDDING_DIM

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("API Embedding 将在 M1 实现（分批 + 重试 + 维度自检）")

    def embed_query(self, text: str) -> list[float]:
        raise NotImplementedError("API Embedding 将在 M1 实现")


class LocalBGEEmbedder:
    """本地 sentence-transformers BGE 实现（选型 Q1 确定后实现）。"""

    dim = settings.EMBEDDING_DIM

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("本地 BGE 将在选型确定后实现")

    def embed_query(self, text: str) -> list[float]:
        raise NotImplementedError("本地 BGE 将在选型确定后实现")


class OllamaEmbedder:
    """Ollama 本地服务实现（后续离线演进项，§16）。"""

    dim = settings.EMBEDDING_DIM

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("Ollama Embedding 后续演进实现")

    def embed_query(self, text: str) -> list[float]:
        raise NotImplementedError("Ollama Embedding 后续演进实现")
