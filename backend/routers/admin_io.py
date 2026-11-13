"""管理端 I/O 运行日志查询（v3 / WP3）。

- 仅管理员可访问；按 trace_id 检索是全链路排障主入口。
- 权限与保留期由 services/io_log 决定，本层只做参数校验与统一包络。
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query

from ..errors import BizError, ok, ERR_PARAM
from ..services import io_log, export_store
from ..services.auth_deps import require_admin

logger = logging.getLogger("rag.router.admin_io")

router = APIRouter(prefix="/api/admin/io-logs", tags=["admin-io"])


def _date(value: Optional[str], field: str) -> Optional[str]:
    if value is None or value == "":
        return None
    text = value.strip()
    if len(text) not in (10, 19):
        raise BizError(ERR_PARAM, f"{field} 需为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS", http_status=400)
    return text


@router.get("")
async def list_io_logs(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    user_id: Optional[int] = Query(None, ge=1),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    status: Optional[str] = Query(None, pattern="^(ok|abort|error|timeout|rejected)$"),
    intent: Optional[str] = Query(None, max_length=32),
    answer_mode: Optional[str] = Query(None, max_length=16),
    keyword: Optional[str] = Query(None, max_length=128),
    trace_id: Optional[str] = Query(None, max_length=32),
    _: dict = Depends(require_admin),
):
    """GET /api/admin/io-logs —— 分页查询；筛选：用户/时间/状态/意图/模式/关键词/trace_id。"""
    total, items = await io_log.list_logs(
        user_id=user_id,
        start=_date(start, "start"),
        end=_date(end, "end"),
        status=status,
        intent=intent,
        answer_mode=answer_mode,
        keyword=keyword,
        trace_id=trace_id,
        page=page,
        size=size,
    )
    return ok({"total": total, "list": items, "page": page, "size": size})


@router.get("/stats")
async def io_log_stats(_: dict = Depends(require_admin)):
    """GET /api/admin/io-logs/stats —— 今日请求数 / 成功率 / P95 首字 / token / 链路占比。"""
    return ok(await io_log.stats())


@router.get("/export")
async def export_io_logs(
    user_id: Optional[int] = Query(None, ge=1),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    status: Optional[str] = Query(None, pattern="^(ok|abort|error|timeout|rejected)$"),
    intent: Optional[str] = Query(None, max_length=32),
    answer_mode: Optional[str] = Query(None, max_length=16),
    keyword: Optional[str] = Query(None, max_length=128),
    trace_id: Optional[str] = Query(None, max_length=32),
    admin: dict = Depends(require_admin),
):
    """GET /api/admin/io-logs/export —— 按同样筛选导出 CSV（上限 1 万行），返回可重复下载链接。"""
    rows = await io_log.list_for_export(
        user_id=user_id, start=_date(start, "start"), end=_date(end, "end"),
        status=status, intent=intent, answer_mode=answer_mode,
        keyword=keyword, trace_id=trace_id,
    )
    if not rows:
        raise BizError(ERR_PARAM, "当前筛选条件没有可导出的日志", http_status=400)
    columns = [
        {"key": "created_at", "title": "时间"},
        {"key": "trace_id", "title": "trace_id"},
        {"key": "user_id", "title": "用户"},
        {"key": "session_id", "title": "会话"},
        {"key": "intent", "title": "意图"},
        {"key": "answer_mode", "title": "链路"},
        {"key": "status", "title": "状态"},
        {"key": "elapsed_ms", "title": "耗时(ms)"},
        {"key": "first_token_ms", "title": "首字(ms)"},
        {"key": "total_tokens", "title": "token"},
        {"key": "query", "title": "问题"},
        {"key": "answer", "title": "回答"},
    ]
    dict_rows = [
        {
            "created_at": r[0].isoformat(sep=" ") if hasattr(r[0], "isoformat") else r[0],
            "trace_id": r[1], "user_id": r[2], "session_id": r[3], "intent": r[4],
            "answer_mode": r[5], "status": r[6], "elapsed_ms": r[7],
            "first_token_ms": r[8], "total_tokens": r[9],
            "query": r[10], "answer": r[11],
        }
        for r in rows
    ]
    # CSV 落盘在同一次请求内完成，行数上限 1 万保证耗时有界；下载令牌复用现有 export_store
    info = await export_store.create_export(admin["user_id"], "io-logs", columns, dict_rows)
    if not info:
        raise BizError(ERR_PARAM, "导出生成失败，请缩小筛选范围后重试", http_status=500)
    return ok({**info, "capped": len(rows) >= 10000}, "导出已生成")


@router.get("/{trace_id}")
async def get_io_detail(trace_id: str, _: dict = Depends(require_admin)):
    """GET /api/admin/io-logs/{trace_id} —— 该轮详情 + 同一 trace 的全部 LLM 调用时间线。"""
    item = await io_log.get_detail(trace_id)
    if item is None:
        raise BizError(ERR_PARAM, "未找到该 trace_id 的运行日志", http_status=404)
    return ok(item)
