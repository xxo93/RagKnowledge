"""FastAPI 认证依赖：后端统一鉴权；前端隐藏按钮不作为安全边界。"""
from __future__ import annotations

from fastapi import Depends, Request

from errors import BizError, ERR_UNAUTHORIZED, ERR_FORBIDDEN
from services import account_store
from services.security import hash_token

COOKIE_NAME = "rag_session"

# 登录态有效期（秒）；剩余不足一半时由仓储层滑动续期
SESSION_SECONDS = 7 * 24 * 60 * 60

# 注意：request 必须显式标注为 fastapi.Request。缺少注解时 FastAPI 会把它当成
# 必填 query 参数，导致所有受保护接口在参数校验阶段直接返回 40001，鉴权逻辑根本不会执行。


async def get_current_user(request: Request) -> dict:
    """解析 HttpOnly Cookie 并校验登录态；失败返回 40101。"""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise BizError(ERR_UNAUTHORIZED, "请先登录", http_status=401)

    session = await account_store.get_active_session(
        hash_token(token), renew_ttl_seconds=SESSION_SECONDS
    )
    if not session:
        raise BizError(ERR_UNAUTHORIZED, "登录态失效，请重新登录", http_status=401)
    if session.get("status") != 1:
        raise BizError(ERR_UNAUTHORIZED, "账号已禁用，请联系管理员", http_status=401)
    return session


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """要求当前用户角色为 admin；普通用户调用管理接口返回 40303。"""
    if user.get("role") != "admin":
        raise BizError(ERR_FORBIDDEN, "需要管理员权限", http_status=403)
    return user
