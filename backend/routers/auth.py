"""认证路由：登录、当前用户、登出、本人改密。

Cookie 为 HttpOnly；服务端只保存令牌哈希，登出/改密/禁用时吊销数据库记录。
时间基准统一由数据库 now() 计算（见 account_store），避免应用 UTC 与库本地时区混用。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from ..errors import BizError, ERR_PARAM, ERR_UNAUTHORIZED, ok
from ..config import settings
from ..services import account_store, audit, security
from ..services.auth_deps import COOKIE_NAME, SESSION_SECONDS, get_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])

PASSWORD_MIN_LEN = 8
PASSWORD_MAX_LEN = 128


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=PASSWORD_MAX_LEN)
    new_password: str = Field(min_length=PASSWORD_MIN_LEN, max_length=PASSWORD_MAX_LEN)


class ProfileUpdateRequest(BaseModel):
    """昵称：1–32 个字符，服务端再去首尾空白并拒绝纯空白。"""

    display_name: str = Field(min_length=1, max_length=32)


def _public_user(user: dict) -> dict:
    return {
        "user_id": user["user_id"],
        "username": user["username"],
        "display_name": user["display_name"],
        "role": user["role"],
    }


def _set_session_cookie(response: Response, token: str) -> None:
    """HttpOnly + SameSite=Lax；本地 HTTP 下不设 Secure，HTTPS 部署时应打开。"""
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
    )


@router.post("/login")
async def login(request: Request, response: Response, body: LoginRequest):
    """POST /api/auth/login —— 用户名密码登录并下发 HttpOnly Cookie。"""
    cred = await account_store.get_credentials_by_username(body.username)
    if cred is None or not security.verify_password(body.password, cred.get("password_hash") or ""):
        # 统一提示，避免通过错误文案枚举用户名；审计同样不区分“用户不存在/密码错误”
        await audit.log_operation(
            None, audit.AUTH_LOGIN, "user", body.username, 0, {"reason": "bad_credentials"}
        )
        raise BizError(ERR_UNAUTHORIZED, "用户名或密码错误", http_status=401)

    user = await account_store.get_user_by_username(body.username)
    if user is None or user.get("status") != 1:
        await audit.log_operation(
            user["user_id"] if user else None, audit.AUTH_LOGIN, "user", body.username, 0,
            {"reason": "disabled"},
        )
        raise BizError(ERR_UNAUTHORIZED, "账号已禁用，请联系管理员", http_status=401)

    token = security.generate_token()
    await account_store.create_auth_session(
        security.hash_token(token),
        user["user_id"],
        SESSION_SECONDS,
        user_agent=request.headers.get("user-agent"),
        client_ip=request.client.host if request.client else None,
    )
    await account_store.touch_last_login(user["user_id"])
    await audit.log_operation(
        user["user_id"], audit.AUTH_LOGIN, "user", user["user_id"], 1,
        {"username": user["username"], "role": user["role"]},
    )
    _set_session_cookie(response, token)
    return ok(_public_user(user), "登录成功")


@router.post("/logout")
async def logout(request: Request, response: Response):
    """POST /api/auth/logout —— 吊销当前登录 Cookie（未登录也返回成功，幂等）。"""
    token = request.cookies.get(COOKIE_NAME)
    if token:
        await account_store.revoke_auth_session(security.hash_token(token))
    response.delete_cookie(COOKIE_NAME, path="/")
    return ok({}, "已退出登录")


@router.get("/me")
async def me(user: dict = Depends(get_current_user)):
    """GET /api/auth/me —— 返回当前登录用户信息。"""
    return ok(_public_user(user))


@router.get("/demo-hint")
async def demo_hint():
    """GET /api/auth/demo-hint —— 登录页演示账号提示（默认关闭的显式开关）。

    仅当 DEMO_CREDENTIAL_HINT=true **且** 配置的默认密码确实能登录时才返回明文；
    否则一律返回 {"enabled": false}，不泄露任何账号信息。
    """
    disabled = {"enabled": False}
    if not settings.DEMO_CREDENTIAL_HINT:
        return ok(disabled)

    username = (settings.DEFAULT_ADMIN_USERNAME or "admin").strip().lower()
    password = (settings.DEFAULT_ADMIN_PASSWORD or "").strip()
    if not password:
        return ok(disabled)

    try:
        user = await account_store.get_user_by_username(username)
        cred = await account_store.get_credentials_by_username(username)
        if (
            user is None
            or user.get("status") != 1
            or user.get("role") != "admin"
            or cred is None
            or not security.verify_password(password, cred.get("password_hash") or "")
        ):
            # 密码被改 / 账号被禁用 / 角色不符：不展示，避免给出过期凭据
            return ok(disabled)
    except Exception:  # noqa: BLE001 - 数据库不可用时登录页仍要能打开
        return ok(disabled)

    return ok({"enabled": True, "username": username, "password": password})


@router.post("/password")
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """POST /api/auth/password —— 本人改密；成功后吊销除当前会话外的其他登录态。"""
    cred = await account_store.get_credentials_by_id(user["user_id"])
    if cred is None or not security.verify_password(body.old_password, cred.get("password_hash") or ""):
        raise BizError(ERR_UNAUTHORIZED, "原密码不正确", http_status=400)

    if body.old_password == body.new_password:
        raise BizError(ERR_PARAM, "新密码不能与原密码相同", http_status=400)

    await account_store.update_password(user["user_id"], security.hash_password(body.new_password))

    token = request.cookies.get(COOKIE_NAME)
    revoked = await account_store.revoke_sessions_for_user(
        user["user_id"], keep_token_hash=security.hash_token(token) if token else None
    )
    await audit.log_operation(
        user["user_id"], audit.AUTH_PASSWORD, "user", user["user_id"], 1,
        {"revoked_sessions": revoked},
    )
    return ok({"revoked_sessions": revoked}, "密码已修改")


@router.patch("/profile")
async def update_profile(
    body: ProfileUpdateRequest,
    user: dict = Depends(get_current_user),
):
    """PATCH /api/auth/profile —— 修改本人昵称（display_name）。"""
    name = body.display_name.strip()
    if not name:
        raise BizError(ERR_PARAM, "昵称不能为空", http_status=400)
    if any(ord(ch) < 32 for ch in name):
        raise BizError(ERR_PARAM, "昵称不能包含控制字符", http_status=400)
    if name == (user.get("display_name") or ""):
        return ok(_public_user(user), "昵称未变化")

    updated = await account_store.update_display_name(user["user_id"], name)
    if not updated:
        raise BizError(ERR_UNAUTHORIZED, "账号不可用，请重新登录", http_status=401)

    await audit.log_operation(
        user["user_id"], audit.AUTH_PROFILE, "user", user["user_id"], 1,
        {"before": user.get("display_name"), "after": name},
    )
    fresh = await account_store.get_user_by_id(user["user_id"])
    return ok(_public_user(fresh or {**user, "display_name": name}), "昵称已更新")
