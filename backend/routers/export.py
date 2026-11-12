"""大结果导出接口（M5）。

- `GET /api/export/{token}`：下载已生成的 CSV（UTF-8 BOM，Excel 可直接打开），有效期内可重复下载。
- 校验顺序：登录态 → 令牌存在 → 归属用户 → 未过期（未设置有效期则视为永不失效） → 文件仍在。
- 失败码：过期/不存在 → 40402；他人令牌 → 40303；未登录 → 40101。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from ..errors import BizError, ERR_FORBIDDEN, ERR_SESSION_NOT_FOUND
from ..services import audit, export_store
from ..services.auth_deps import get_current_user

router = APIRouter(prefix="/api/export", tags=["export"])

_MESSAGES = {
    "missing": "下载链接不存在或文件已被清理，请重新发起查询获取新链接",
    "expired": "下载链接已过期，请重新发起查询获取新链接",
    "forbidden": "该下载链接不属于当前登录账号",
}


@router.get("/{token}")
async def download(token: str, user: dict = Depends(get_current_user)):
    status, record = await export_store.resolve(token, int(user["user_id"]))
    if status != "ok" or not record:
        await audit.log_operation(
            user.get("user_id"), audit.BIZ_EXPORT, target_type="export_token", target_id=None,
            result=0, detail={"stage": "download", "status": status},
        )
        code = ERR_FORBIDDEN if status == "forbidden" else ERR_SESSION_NOT_FOUND
        http_status = 403 if status == "forbidden" else 404
        raise BizError(code, _MESSAGES.get(status, _MESSAGES["missing"]), http_status=http_status)

    path = Path(record["file_path"])
    await export_store.mark_downloaded(record["token_hash"])
    await audit.log_operation(
        user.get("user_id"), audit.BIZ_EXPORT, target_type="export_token", target_id=None,
        result=1, detail={"stage": "download", "filename": record["filename"], "rows": record["row_count"]},
    )
    return FileResponse(
        path,
        media_type="text/csv; charset=utf-8",
        filename=record["filename"],
        headers={"Cache-Control": "no-store"},
    )
