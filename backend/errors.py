"""业务异常体系与全局错误处理器。

约定：
- 非 SSE 接口统一返回 `{"code": int, "msg": str, "data": any}`，成功 code=0；
- 业务层（services/agent）只抛 BizError，不直接使用 FastAPI 的 HTTPException。
- 全局处理器负责把异常转成规范 JSON，且不向前端暴露堆栈、SQL 与文件绝对路径。
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

# 业务错误码；HTTP 状态由 BizError.http_status 单独控制
OK = 0
ERR_PARAM = 40001            # 参数校验失败
ERR_FILE_TYPE = 40002        # 文件类型不支持
ERR_FILE_SIZE = 40003         # 文件超限
ERR_DOC_NOT_FOUND = 40401
ERR_SESSION_NOT_FOUND = 40402
ERR_USER_NOT_FOUND = 40403
ERR_TRACE_NOT_FOUND = 40405  # 运行日志 trace 不存在或不属于当前用户
ERR_RECORD_NOT_FOUND = 40406  # 回答记录不存在或不属于当前用户（反馈/导出）
ERR_UNAUTHORIZED = 40101       # 未登录或登录态失效
ERR_FORBIDDEN = 40303          # 已登录但无角色权限
ERR_USER_EXISTS = 40004        # 用户名已存在 / 账号规则冲突
ERR_DB_UNAVAILABLE = 50001   # 数据库连接池耗尽 / 连接中断
ERR_LLM_UNAVAILABLE = 50002  # LLM 超时 / 鉴权失败
ERR_INGEST_FAILED = 50003     # 文档解析 / 切片 / 向量化失败
ERR_QUOTA_EXCEEDED = 42901    # 每用户日 token 配额耗尽
ERR_RATE_LIMITED = 42902      # 每用户每分钟请求数超限


class BizError(Exception):
    """可预期的业务异常。

    code: 业务错误码（包络里的 code）
    msg: 面向用户的友好提示（不含内部细节）
    http_status: HTTP 状态码
    """

    def __init__(self, code: int, msg: str, http_status: int = 400) -> None:
        self.code = code
        self.msg = msg
        self.http_status = http_status
        super().__init__(msg)


def ok(data=None, msg: str = "ok") -> dict:
    """成功响应包络。"""
    return {"code": OK, "msg": msg, "data": data if data is not None else {}}


def _error_envelope(code: int, msg: str) -> dict:
    return {"code": code, "msg": msg, "data": {}}


def register_exception_handlers(app: FastAPI) -> None:
    """把异常处理器挂到 FastAPI 应用上（在 main.py 启动时调用一次）。"""

    @app.exception_handler(BizError)
    async def _handle_biz(_: Request, exc: BizError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=_error_envelope(exc.code, exc.msg))

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # 不回显原始校验细节中的内部路径，仅汇总首条原因，保证前端提示简洁
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", []))
        msg = f"参数校验失败：{loc} {first.get('msg', '')}".strip()
        return JSONResponse(status_code=400, content=_error_envelope(ERR_PARAM, msg))

    @app.exception_handler(Exception)
    async def _handle_unknown(request: Request, exc: Exception) -> JSONResponse:
        # 兜底：记录完整堆栈由 logging 中间件负责；这里只给通用提示，不泄露内部信息
        return JSONResponse(
            status_code=500,
            content=_error_envelope(50099, "服务内部错误，请稍后重试"),
        )
