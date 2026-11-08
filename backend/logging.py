"""日志与 request_id 中间件（架构文档 §8.3）。

- 开发期以可读格式输出；关键埋点（上传耗时、问答耗时、工具轮次）在各服务层显式调用。
- 中间件为每个请求生成 request_id，写入响应头 X-Request-Id 与上下文，便于排障关联。
- 敏感字段（LLM_API_KEY、DB_DSN 密码、文档全文）禁止打印。
"""
from __future__ import annotations

import logging
import uuid
import time

from fastapi import Request, FastAPI
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware

from config import settings

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def setup_logging() -> None:
    """初始化根日志级别。重复调用幂等。"""
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(level=level, format=LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """注入 request_id，并记录请求方法、路径、耗时、状态码。"""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:12]
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger = logging.getLogger("rag.request")
            logger.exception("未捕获异常 path=%s method=%s", request.url.path, request.method)
            raise
        finally:
            pass
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        response.headers["X-Request-Id"] = request_id
        logger = logging.getLogger("rag.request")
        logger.info(
            "%s %s -> %s | %dms",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response


def register_middlewares(app: FastAPI) -> None:
    setup_logging()
    app.add_middleware(RequestContextMiddleware)
