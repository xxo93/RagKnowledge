"""请求日志与 request_id / trace_id 中间件。

- 开发期以可读格式输出；关键埋点（上传耗时、问答耗时、工具轮次）在各服务层显式调用。
- 中间件为每个请求生成 request_id，写入响应头 X-Request-Id。
- v3 / WP3：读入前端 `X-Trace-Id`（缺失则生成），写入 contextvar 与响应头，
  供 LLM 调用、工具链、异常与 SSE done 帧共用同一个 trace_id 做全链路排障。
- 敏感字段（LLM_API_KEY、DB_DSN 密码、文档全文）禁止打印。
"""
from __future__ import annotations

import logging
import time

from fastapi import Request, FastAPI
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware

from .config import settings
from .services import io_log

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
TRACE_HEADER = "X-Trace-Id"
MAX_TRACE_LEN = 32
_TRACE_ALLOWED = set("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_")


def setup_logging() -> None:
    """初始化根日志级别。重复调用幂等。"""
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(level=level, format=LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")


def normalize_trace_id(raw: str | None) -> str:
    """只接受 8–32 位安全字符，异常输入直接换成新值（防日志注入）。"""
    if raw:
        value = raw.strip()
        if 8 <= len(value) <= MAX_TRACE_LEN and all(ch in _TRACE_ALLOWED for ch in value):
            return value
    return io_log.new_trace_id()


class RequestContextMiddleware(BaseHTTPMiddleware):
    """注入 request_id / trace_id，并记录请求方法、路径、耗时、状态码。"""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-Id") or io_log.new_trace_id()[:12]
        trace_id = normalize_trace_id(request.headers.get(TRACE_HEADER))
        token = io_log.set_trace_id(trace_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger = logging.getLogger("rag.request")
            logger.exception("未捕获异常 path=%s trace_id=%s", request.url.path, trace_id)
            raise
        finally:
            io_log.reset_trace_id(token)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        response.headers["X-Request-Id"] = request_id
        response.headers[TRACE_HEADER] = trace_id
        logger = logging.getLogger("rag.request")
        logger.info(
            "%s %s -> %s | %dms | trace=%s",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            trace_id,
        )
        return response


def register_middlewares(app: FastAPI) -> None:
    setup_logging()
    app.add_middleware(RequestContextMiddleware)
