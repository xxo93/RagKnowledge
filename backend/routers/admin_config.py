"""管理员配置中心路由（M1）。

GET  /api/admin/config           读取全量配置（含默认值/范围/是否被库覆盖）
PUT  /api/admin/config           批量保存（整批校验，任一非法整批拒绝）
POST /api/admin/config/reset     恢复默认（单 key / 单分组 / 全部）

安全约束：
- 全部 require_admin；member 调用返回 40303，不依赖前端隐藏入口；
- 只接收 registry（services/app_config.PARAMS）里登记过的 key，未知 key 直接 40001；
- 密钥类参数（API Key / DSN）不在本 registry 内，因此无法经此接口读写；
- 保存与恢复默认都写 operation_log（action=CONFIG_UPDATE）。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..errors import ok
from ..services import app_config
from ..services.auth_deps import require_admin

router = APIRouter(prefix="/api/admin", tags=["admin-config"])


class SaveConfigRequest(BaseModel):
    # 只允许标量；嵌套结构（例如意图注册表）走单独的导入接口
    values: dict[str, Any] = Field(default_factory=dict)


class ResetConfigRequest(BaseModel):
    keys: Optional[list[str]] = None
    group: Optional[str] = None


@router.get("/config")
async def get_config(_: dict = Depends(require_admin)):
    """返回分组后的配置快照，前端据此渲染表单。"""
    return ok(await app_config.load_snapshot())


@router.put("/config")
async def save_config(body: SaveConfigRequest, user: dict = Depends(require_admin)):
    """批量保存；成功后立即刷新缓存，下一条请求即生效。"""
    return ok(await app_config.set_values(body.values, actor_user_id=user["user_id"]))


@router.post("/config/reset")
async def reset_config(body: ResetConfigRequest, user: dict = Depends(require_admin)):
    """恢复默认：删除库中覆盖行，回退到 .env / 代码默认值。"""
    return ok(
        await app_config.reset(
            keys=body.keys,
            group=body.group,
            actor_user_id=user["user_id"],
        )
    )
