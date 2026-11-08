"""全局配置读取（需求文档 §五.3 / 架构文档 §8.1）。

设计原则：
- 配置来源优先级：环境变量 > .env 文件 > 代码默认值（pydantic-settings 保证）。
- 禁止在业务代码里硬编码密钥、模型名、切片参数、Top-K、向量维度、连接串。
- 运行目录约定为 backend/（`uvicorn main:app` 在 backend 下启动）；
  .env 可放在项目根（../.env）或 backend 本地（./.env），按顺序读取。
"""
from __future__ import annotations

from pathlib import Path
from functools import lru_cache

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ 目录 = 本文件所在目录；项目根 = 其上一级
BACKEND_DIR: Path = Path(__file__).resolve().parent
ROOT_DIR: Path = BACKEND_DIR.parent


class Settings(BaseSettings):
    """应用配置。字段命名与 .env 中的大写变量自动对应（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),  # 先读项目根 .env，再读 backend/.env
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------- 服务 ----------
    APP_HOST: str = "127.0.0.1"          # 架构 §6.4：无鉴权，只监听本机
    APP_PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    CORS_ORIGINS: str = "http://127.0.0.1:5173,http://localhost:5173"

    # ---------- 数据库（PostgreSQL + pgvector，单库） ----------
    DB_DSN: str = "postgresql://postgres:postgres@127.0.0.1:5432/rag_agent"
    DB_POOL_MIN: int = 2
    DB_POOL_MAX: int = 10

    # ---------- LLM（OpenAI 兼容协议，Provider 可配，ADR-009） ----------
    LLM_PROVIDER: str = "deepseek"
    LLM_BASE_URL: str = "https://api.deepseek.com"
    LLM_API_KEY: str = ""                # 必填，骨架阶段允许为空；M2 真实调用前校验
    LLM_MODEL: str = "deepseek-chat"
    LLM_TIMEOUT: int = 60

    # ---------- Embedding（适配器模式，ADR-008；选型未定先给默认） ----------
    EMBEDDING_PROVIDER: str = "api"      # local / api / ollama
    EMBEDDING_MODEL: str = "BAAI/bge-m3"
    EMBEDDING_DIM: int = 1024           # 【高危】必须与 document_chunk.embedding vector(N) 严格一致
    EMBEDDING_BATCH_SIZE: int = 32
    EMBEDDING_API_BASE_URL: str = ""
    EMBEDDING_API_KEY: str = ""

    # ---------- 切片 ----------
    CHUNK_SIZE: int = 500
    CHUNK_OVERLAP: int = 100

    # ---------- 检索 ----------
    TOP_K: int = 5
    RETRIEVAL_MIN_SCORE: float = 0.0
    RERANK_ENABLED: bool = False
    RERANK_MODEL: str = "BAAI/bge-reranker-v2-m3"
    HNSW_EF_SEARCH: int = 40
    # pgvector 0.8+ 取值：off / relaxed_order / strict_order；0.7.x 无此项，代码需容错
    HNSW_ITERATIVE_SCAN: str = "relaxed_order"
    HNSW_MAX_SCAN_TUPLES: int = 20000

    # ---------- Agent ----------
    HISTORY_ROUNDS: int = 5              # 最近 N 轮（一问一答为一轮）
    MAX_TOOL_ROUNDS: int = 3              # 循环上限，防止死循环（需求 F-06）

    # ---------- 文件上传 ----------
    UPLOAD_MAX_MB: int = 10
    UPLOAD_DIR_NAME: str = "data/uploads"

    # ---------- 派生路径 / 列表 ----------
    @computed_field  # type: ignore[prop-decorator]
    @property
    def upload_dir(self) -> Path:
        p = ROOT_DIR / self.UPLOAD_DIR_NAME
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def upload_max_bytes(self) -> int:
        return self.UPLOAD_MAX_MB * 1024 * 1024

    def safety_check(self) -> list[str]:
        """启动阶段的软性校验，返回警告信息列表（不直接抛错，便于骨架独立启动）。"""
        warnings: list[str] = []
        if not self.LLM_API_KEY:
            warnings.append("未配置 LLM_API_KEY，LLM 生成能力不可用（仅影响 M2 之后链路）")
        if self.EMBEDDING_DIM <= 0:
            warnings.append("EMBEDDING_DIM 必须为正整数")
        if self.CHUNK_OVERLAP >= self.CHUNK_SIZE:
            warnings.append("CHUNK_OVERLAP 应小于 CHUNK_SIZE，否则切片退化为重复窗口")
        return warnings


@lru_cache
def get_settings() -> Settings:
    """单例配置。各处使用 `from config import settings` 即可。"""
    return Settings()


settings: Settings = get_settings()
