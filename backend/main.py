"""FastAPI 应用入口（架构 §2.3 / §12 启动序列）。

启动方式（在 backend/ 目录下）：
    uvicorn main:app --reload --host 127.0.0.1 --port 8000
在线接口文档：http://127.0.0.1:8000/docs

职责：
- lifespan：启动初始化数据库连接池（失败降级不崩）、关闭时回收；
- CORS：仅放行本地 Vite 开发源（§6.4）；
- 注册全局异常处理器、request_id 中间件、四个路由模块；
- 启动时输出配置警告（缺 Key、维度异常等），不做阻断式强校验（骨架阶段可独立启动）。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from errors import register_exception_handlers, ok
from logging import register_middlewares
from db import init_pool, close_pool, db_ready
from routers import system, docs, session, chat

logger = logging.getLogger("rag.main")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # ---- 启动：中间件在模块级注册；此处只做资源初始化 ----
    for w in settings.safety_check():
        logger.warning("配置提示：%s", w)
    await init_pool()
    logger.info(
        "应用启动：db_ready=%s | LLM=%s | EMBEDDING_DIM=%d | TOP_K=%d",
        db_ready,
        settings.LLM_MODEL,
        settings.EMBEDDING_DIM,
        settings.TOP_K,
    )
    yield
    # ---- 关闭 ----
    await close_pool()
    logger.info("应用已关闭，连接池已回收")


app = FastAPI(
    title="企业文档智能问答 Agent（RAG + LangGraph）",
    version="0.1.0-skeleton",
    description="私有文档知识库 + 多工具 Agent Demo；骨架阶段，接口按里程碑逐步接通。",
    lifespan=lifespan,
)

# CORS（开发期 Vite 代理 / 浏览器直连双兜底；只放行本地源）
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

register_exception_handlers(app)
register_middlewares(app)

# 路由注册（前缀已在各 router 内定义为 /api/...）
app.include_router(system.router)
app.include_router(docs.router)
app.include_router(session.router)
app.include_router(chat.router)


@app.get("/health", tags=["meta"])
async def health():
    """轻量健康检查，不依赖数据库。"""
    return ok({"status": "up", "db_ready": db_ready, "stage": "skeleton"})


@app.get("/", tags=["meta"])
async def index():
    return ok({
        "name": "RagKnowledge",
        "docs": "/docs",
        "health": "/health",
        "info": "/api/system/info",
        "stage": "skeleton",
    })
