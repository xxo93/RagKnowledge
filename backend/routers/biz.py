"""业务库查询接口。

- `GET /api/biz/intents`：意图注册表只读展示（admin 可见完整定义；member 只看能力清单与示例）；
- `POST /api/biz/query`：直接执行一次查询（供前端"业务查询"面板与验收脚本使用）；
- 权限/参数/合规/脱敏/行数限制全部复用 `services.biz_query`，本路由不拼接 SQL。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..errors import BizError, ok, ERR_FORBIDDEN, ERR_PARAM
from ..services import biz_query
from ..services.auth_deps import get_current_user, require_admin

router = APIRouter(prefix="/api/biz", tags=["biz"])


class BizQueryRequest(BaseModel):
    query: str = Field("", max_length=500)
    intent: str | None = Field(None, max_length=64)
    args: dict | None = None


@router.get("/intents")
async def list_intents(user: dict = Depends(get_current_user)):
    """意图清单：admin 返回完整参数与列定义，member 只返回标题与示例。"""
    items = biz_query.list_intents()
    if user.get("role") != "admin":
        items = [{"intent_id": i["intent_id"], "title": i["title"], "examples": i["examples"]} for i in items]
    return ok({"intents": items, "ready": biz_query.is_ready()})


@router.get("/intents/detail")
async def list_intents_detail(_: dict = Depends(require_admin)):
    """管理员用完整注册表定义（含 SQL 模板），只读展示。"""
    return ok({"intents": biz_query.list_intents()})


@router.post("/query")
async def run_query(body: BizQueryRequest, user: dict = Depends(get_current_user)):
    """执行查询：给了 intent 走直查（仍需权限与合规校验），否则先识别意图。"""
    if body.intent:
        result = await biz_query.execute(body.intent, body.args or {}, user)
    else:
        if not body.query.strip():
            raise BizError(ERR_PARAM, "query 不能为空", http_status=400)
        result = await biz_query.decide_and_execute(body.query.strip(), user)
    if not result.get("ok"):
        code = result.get("code") or "BIZ_ERROR"
        if code == "BIZ_FORBIDDEN":
            raise BizError(ERR_FORBIDDEN, result.get("message") or "无权限", http_status=403)
        raise BizError(ERR_PARAM, result.get("message") or "查询未执行", http_status=400)
    return ok(result)
