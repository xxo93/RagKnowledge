"""管理员提示词版本管理（v3 / WP5-7）。

接口：
- GET   /api/admin/prompts                      列表：当前版本、灰度、历史版本
- POST  /api/admin/prompts/{name}/versions      发新版本（可带灰度）
- PATCH /api/admin/prompts/{name}/gray          调整当前版本灰度
- POST  /api/admin/prompts/{name}/activate      启用/回滚到指定版本（默认 100%）

提示词内容只影响登记表内的 4 个提示词；改错可通过 activate 一键回滚。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..errors import ok
from ..services import prompt_store
from ..services.auth_deps import require_admin

router = APIRouter(prefix="/api/admin/prompts", tags=["admin-prompts"])


class PromptVersionCreate(BaseModel):
    content: str = Field(min_length=1, max_length=8000)
    gray_percent: int = Field(default=100, ge=0, le=100)


class PromptGrayUpdate(BaseModel):
    gray_percent: int = Field(ge=0, le=100)


class PromptActivate(BaseModel):
    version: int = Field(ge=1)
    gray_percent: int = Field(default=100, ge=0, le=100)


@router.get("")
async def list_prompts(_: dict = Depends(require_admin)):
    items = await prompt_store.list_prompts()
    return ok({"items": items, "cache_ttl_seconds": prompt_store.CACHE_TTL_SECONDS})


@router.post("/{name}/versions")
async def create_version(
    name: str,
    body: PromptVersionCreate,
    user: dict = Depends(require_admin),
):
    result = await prompt_store.create_version(
        name, body.content, body.gray_percent, user.get("user_id")
    )
    return ok(result, msg="新版本已创建并设为当前版本")


@router.patch("/{name}/gray")
async def update_gray(
    name: str,
    body: PromptGrayUpdate,
    user: dict = Depends(require_admin),
):
    result = await prompt_store.set_gray(name, body.gray_percent, user.get("user_id"))
    return ok(result, msg="灰度比例已更新")


@router.post("/{name}/activate")
async def activate(
    name: str,
    body: PromptActivate,
    user: dict = Depends(require_admin),
):
    result = await prompt_store.activate(
        name, body.version, body.gray_percent, user.get("user_id")
    )
    return ok(result, msg="已切换当前版本")
