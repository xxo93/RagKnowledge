"""全局应用配置读取。

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
    APP_HOST: str = "127.0.0.1"          # 服务仅监听本机回环地址
    APP_PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    CORS_ORIGINS: str = "http://127.0.0.1:5173,http://localhost:5173"

    # ---------- 数据库（PostgreSQL + pgvector，单库） ----------
    DB_DSN: str = "postgresql://postgres:postgres@127.0.0.1:5432/rag_agent"
    DB_POOL_MIN: int = 2
    DB_POOL_MAX: int = 10

    # ---------- LLM（OpenAI 兼容协议，Provider 可配置） ----------
    LLM_PROVIDER: str = "deepseek"
    LLM_BASE_URL: str = "https://api.deepseek.com"
    LLM_API_KEY: str = ""                # 未配置时 LLM 生成能力不可用
    LLM_MODEL: str = "deepseek-chat"
    LLM_TIMEOUT: int = 60
    # plan 是否优先使用 LLM 结构化决策；False 时回退确定性关键词决策
    LLM_PLAN_ENABLED: bool = True

    # ---------- Embedding（适配器模式；API 文本向量模型） ----------
    EMBEDDING_PROVIDER: str = "api"      # local / api / ollama
    EMBEDDING_MODEL: str = "text-embedding-v3"
    EMBEDDING_DIM: int = 1024           # 必须与 document_chunk.embedding 的 vector(N) 一致
    EMBEDDING_BATCH_SIZE: int = 10       # 单批数量还受供应商接口限制约束
    EMBEDDING_API_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    EMBEDDING_API_KEY: str = ""          # 缺失时入库/检索向量化失败并由调用方降级或报错

    # ---------- 切片 ----------
    CHUNK_SIZE: int = 500
    CHUNK_OVERLAP: int = 100

    # ---------- 检索 ----------
    TOP_K: int = 5
    RETRIEVAL_MIN_SCORE: float = 0.0
    RERANK_ENABLED: bool = False
    RERANK_MODEL: str = "gte-rerank-v2"
    RERANK_BASE_URL: str = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
    RERANK_TOP_N: int = 5        # 重排后保留条数；<=0 表示保留全部候选
    RERANK_TIMEOUT: int = 10
    HNSW_EF_SEARCH: int = 40
    # pgvector 0.8+ 取值：off / relaxed_order / strict_order；0.7.x 无此项，代码需容错
    HNSW_ITERATIVE_SCAN: str = "relaxed_order"
    HNSW_MAX_SCAN_TUPLES: int = 20000

    # ---------- Agent ----------
    HISTORY_ROUNDS: int = 5              # 最近 N 轮（一问一答为一轮）
    MAX_TOOL_ROUNDS: int = 3              # 工具循环上限，防止重复回跳死循环
    # assess 后是否允许再规划回跳工具；默认按确定性规则收敛，避免额外 LLM 调用
    MULTI_ROUND_ENABLED: bool = True
    # 是否由 LLM 做再决策；True 会增加 LLM 调用，默认 False
    LLM_REPLAN_ENABLED: bool = False

    # ---------- SSE 心跳 ----------
    # 流前置阶段（决策 LLM / 向量化 / Rerank）静默期发送 SSE 注释帧 ": ping" 的间隔（秒）；
    # 前端 sse.js 对 ":" 开头行直接跳过，不产生业务事件，仅用于防止代理/网关静默断连。
    SSE_HEARTBEAT_SEC: float = 10.0

    # ---------- 文件上传 ----------
    UPLOAD_MAX_MB: int = 10
    UPLOAD_DIR_NAME: str = "data/uploads"

    # ---------- 默认管理员（演示环境开箱即用）----------
    # 由 scripts/migrate_auth_multitenant.py 首次建库与 scripts/ensure_admin.py 使用；
    # 生产部署请用环境变量覆盖，并登录后立即改密（或把 DEFAULT_ADMIN_PASSWORD 置空改为随机生成）。
    DEFAULT_ADMIN_USERNAME: str = "admin"
    DEFAULT_ADMIN_PASSWORD: str = "Admin@12345"
    DEFAULT_ADMIN_DISPLAY_NAME: str = "系统管理员"

    # 是否在登录页显示演示账号提示（默认关闭）。
    # 开启后 GET /api/auth/demo-hint 才会返回明文密码；仅在默认密码确实能登录时才展示，
    # 改了密码或禁用账号后自动隐藏，避免给出过期的错误提示。切勿在生产环境开启。
    DEMO_CREDENTIAL_HINT: bool = False

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
        """启动时执行软性配置检查，返回警告列表；不阻断应用启动。"""
        warnings: list[str] = []
        if not self.LLM_API_KEY:
            warnings.append("未配置 LLM_API_KEY，LLM 生成能力不可用（仅影响 M2 之后链路）")
        if self.EMBEDDING_PROVIDER.lower() == "api" and not self.EMBEDDING_API_KEY:
            warnings.append("EMBEDDING_PROVIDER=api 但未配置 EMBEDDING_API_KEY，向量化（入库/检索）不可用")
        if self.RERANK_ENABLED and not self.EMBEDDING_API_KEY:
            warnings.append("RERANK_ENABLED=true 但未配置 EMBEDDING_API_KEY（Rerank 复用该 Key），重排将回退原序")
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
