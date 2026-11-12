"""管理员用户管理路由。

安全约束：
- 全部接口要求管理员身份；后端强制校验，不依赖前端隐藏入口；
- 禁止禁用自己，禁止禁用最后一个启用中的管理员；
- 重置密码后吊销目标账号全部登录态；
- 审计记录操作类型与结果，不记录密码明文或哈希。
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..errors import BizError, ERR_PARAM, ERR_USER_NOT_FOUND, ok
from ..services import account_store, audit, security
from ..services.auth_deps import require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])

USERNAME_PATTERN = r"^[a-zA-Z0-9_.-]{2,64}$"
PASSWORD_MIN_LEN = 8
PASSWORD_MAX_LEN = 128


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=2, max_length=64, pattern=USERNAME_PATTERN)
    display_name: str = Field(default="", max_length=128)
    password: str = Field(min_length=PASSWORD_MIN_LEN, max_length=PASSWORD_MAX_LEN)
    role: Literal["admin", "member"] = "member"


class UpdateStatusRequest(BaseModel):
    status: Literal[0, 1]


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=PASSWORD_MIN_LEN, max_length=PASSWORD_MAX_LEN)


def _public(item: dict) -> dict:
    return {
        "user_id": item["user_id"],
        "username": item["username"],
        "display_name": item["display_name"],
        "role": item["role"],
        "status": item["status"],
        "created_at": item.get("created_at"),
        "last_login_at": item.get("last_login_at"),
    }


@router.get("/users")
async def list_users(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    keyword: str = Query("", description="按用户名或显示名模糊筛选"),
    role: Optional[Literal["admin", "member"]] = Query(None),
    status: Optional[int] = Query(None, ge=0, le=1),
    _: dict = Depends(require_admin),
):
    """GET /api/admin/users —— 用户列表（不含密码哈希），支持可选服务端筛选。"""
    total, items = await account_store.list_users(
        page,
        size,
        keyword=keyword or None,
        role=role,
        status=status,
    )
    return ok({"total": total, "page": page, "size": size, "list": [_public(i) for i in items]})


@router.post("/users")
async def create_user(body: CreateUserRequest, admin: dict = Depends(require_admin)):
    """POST /api/admin/users —— 创建 member/admin 账号。"""
    user_id = await account_store.create_user(
        body.username,
        body.display_name or body.username,
        security.hash_password(body.password),
        body.role,
    )
    created = await account_store.get_user_by_id(user_id)
    await audit.log_operation(
        admin["user_id"], audit.USER_MANAGE, "user", user_id, 1,
        {"op": "create", "username": body.username, "role": body.role},
    )
    return ok(_public(created), "账号已创建")


@router.patch("/users/{user_id}/status")
async def update_status(
    user_id: int,
    body: UpdateStatusRequest,
    admin: dict = Depends(require_admin),
):
    """PATCH /api/admin/users/{user_id}/status —— 启用/禁用账号（禁用即吊销其全部登录态）。"""
    target = await account_store.get_user_by_id(user_id)
    if target is None:
        raise BizError(ERR_USER_NOT_FOUND, "账号不存在", http_status=404)

    if body.status == 0:
        if user_id == admin["user_id"]:
            raise BizError(ERR_PARAM, "不能禁用自己的账号", http_status=400)
        if target["role"] == "admin" and await account_store.count_enabled_admins(user_id) == 0:
            raise BizError(ERR_PARAM, "不能禁用最后一个启用中的管理员", http_status=400)

    ok_flag = await account_store.set_user_status(user_id, body.status)
    if not ok_flag:
        raise BizError(ERR_USER_NOT_FOUND, "账号不存在", http_status=404)

    await audit.log_operation(
        admin["user_id"], audit.USER_MANAGE, "user", user_id, 1,
        {"op": "enable" if body.status == 1 else "disable", "username": target["username"]},
    )
    updated = await account_store.get_user_by_id(user_id)
    return ok(_public(updated), "账号已启用" if body.status == 1 else "账号已禁用")


@router.post("/users/{user_id}/reset-password")
async def reset_password(
    user_id: int,
    body: ResetPasswordRequest,
    admin: dict = Depends(require_admin),
):
    """POST /api/admin/users/{user_id}/reset-password —— 管理员重置他人密码并吊销其登录态。"""
    target = await account_store.get_user_by_id(user_id)
    if target is None:
        raise BizError(ERR_USER_NOT_FOUND, "账号不存在", http_status=404)

    await account_store.update_password(user_id, security.hash_password(body.new_password))
    revoked = await account_store.revoke_sessions_for_user(user_id)
    await audit.log_operation(
        admin["user_id"], audit.USER_MANAGE, "user", user_id, 1,
        {"op": "reset_password", "username": target["username"], "revoked_sessions": revoked},
    )
    return ok({"user_id": user_id, "revoked_sessions": revoked}, "密码已重置")
