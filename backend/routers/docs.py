"""文档知识库管理员路由。

- POST /api/docs/upload：后缀白名单 + 大小校验后，交 services.ingest.process 完成 解析 → 清洗 → 切片 → 向量化 → T1/T2/T3 事务入库；
- POST /api/docs/upload/batch：服务端顺序处理多文件，聚合 success/failed；
- POST /api/docs/upload/chunk：分块上传，按 upload_id 落盘临时分片（大文件不必一次读进内存）；
- POST /api/docs/upload/complete：合并分片 → 大小/后缀校验 → 入库；
- DELETE /api/docs/upload/chunk：放弃上传并清理临时分片；
- GET  /api/docs/list：分页文档列表，管理员专属；
- DELETE /api/docs/{doc_id}：单事务删除切片与文档；
- POST /api/docs/{doc_id}/reparse：后端运维能力，重读原件并事务替换切片；
- POST /api/docs/reindex：后端运维能力，重建 HNSW 向量索引。
权限：全部写接口与文档列表均 require_admin；每次写操作落 operation_log。
SQL 全部收敛在 services/vector_store.py，路由不拼 SQL。
"""
from __future__ import annotations

import logging
import re
import shutil
import time
from datetime import date
from typing import Optional
from pathlib import Path

from fastapi import APIRouter, Depends, UploadFile, File, Form, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..errors import BizError, ok, ERR_INGEST_FAILED, ERR_DOC_NOT_FOUND
from ..config import settings
from ..services import app_config, audit, ingest, preview as preview_service, vector_store as vs, maintenance
from ..services.auth_deps import get_current_user, require_admin

logger = logging.getLogger("rag.docs")

router = APIRouter(prefix="/api/docs", tags=["docs"])

ALLOWED_EXTS = {".pdf", ".docx", ".md", ".xlsx", ".xlsm", ".xls", ".csv", ".doc"}

# 上传流读取块大小：分块读取即可边读边判超限，避免“先读进内存再校验”
READ_BLOCK_BYTES = 64 * 1024
# upload_id 只允许 UUID 形态，既够随机又天然阻断 ../ 之类的目录穿越
UPLOAD_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
MAX_TOTAL_CHUNKS = 1000  # 1000 片 × 单片上限 32MB = 32GB 理论上限，实际受总大小限制约束


def _validate_upload(filename: str, size: int, max_bytes: Optional[int] = None) -> None:
    """校验文件后缀白名单与大小；校验失败直接拒绝，不落盘、不入库。

    max_bytes 为空时按单次上传上限（UPLOAD_MAX_MB）校验；分块上传合并后按总大小上限校验。
    """
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise BizError(40002, f"仅支持 {', '.join(sorted(ALLOWED_EXTS))} 文件", http_status=400)
    limit = settings.upload_max_bytes if max_bytes is None else max_bytes
    if size > limit:
        limit_mb = limit // (1024 * 1024)
        raise BizError(40003, f"文件超过 {limit_mb}MB 限制，请拆分后上传", http_status=400)


async def _read_capped(file: UploadFile, max_bytes: int, limit_mb: int) -> bytes:
    """分块读取上传流，累计超过 max_bytes 立即中断。

    相比 `await file.read()` 再判断 len()，这里的峰值内存被限制在 max_bytes 内：
    超限请求在读满上限后就抛错，不会先把整个大文件读进内存。
    """
    buf = bytearray()
    while True:
        chunk = await file.read(READ_BLOCK_BYTES)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise BizError(40003, f"文件超过 {limit_mb}MB 限制，请拆分后上传", http_status=400)
    return bytes(buf)


async def _process_single_upload(file: UploadFile) -> dict:
    """单文件统一处理：读取（带超限中断）→ 安全校验 → 入库；供单文件与批量端点复用。"""
    name = file.filename or "未命名文档"
    content = await _read_capped(file, settings.upload_max_bytes, settings.UPLOAD_MAX_MB)
    _validate_upload(name, len(content))
    return await ingest.process(name, content)


@router.post("/upload")
async def upload_doc(file: UploadFile = File(...), user: dict = Depends(require_admin)):
    """POST /api/docs/upload —— 管理员同步处理并入库；返回 {"doc_id","chunk_count","status"}。"""
    result = await _process_single_upload(file)
    await audit.log_operation(
        user["user_id"], audit.DOC_UPLOAD, "document", result["doc_id"], 1,
        {"file_name": file.filename, "chunk_count": result["chunk_count"]},
    )
    return ok(result)


@router.post("/upload/batch")
async def upload_batch(files: list[UploadFile] = File(...), user: dict = Depends(require_admin)):
    """POST /api/docs/upload/batch —— 管理员顺序处理多文件，聚合成功/失败明细。"""
    success: list[dict] = []
    failed: list[dict] = []
    for file in files:
        name = file.filename or "未命名文档"
        try:
            result = await _process_single_upload(file)
            success.append({
                "file_name": name,
                "doc_id": result["doc_id"],
                "chunk_count": result["chunk_count"],
                "status": result["status"],
            })
            await audit.log_operation(
                user["user_id"], audit.DOC_UPLOAD, "document", result["doc_id"], 1,
                {"file_name": name, "chunk_count": result["chunk_count"], "batch": True},
            )
        except BizError as exc:
            failed.append({"file_name": name, "code": exc.code, "error": exc.msg})
            await audit.log_operation(
                user["user_id"], audit.DOC_UPLOAD, "document", None, 0,
                {"file_name": name, "batch": True, "reason": exc.code},
            )
        except Exception:  # noqa: BLE001 - 批量场景单文件失败不中断其余文件
            logger.exception("批量上传处理失败：%s", name)
            failed.append({
                "file_name": name,
                "code": ERR_INGEST_FAILED,
                "error": "文档处理失败，请查看后端日志",
            })
            await audit.log_operation(
                user["user_id"], audit.DOC_UPLOAD, "document", None, 0,
                {"file_name": name, "batch": True, "reason": "exception"},
            )
    return ok({
        "success": success,
        "failed": failed,
        "success_count": len(success),
        "failed_count": len(failed),
    })


def _chunk_folder(upload_id: str) -> Path:
    """解析并校验分片目录；upload_id 非法一律 400，避免路径穿越。"""
    if not UPLOAD_ID_RE.match(upload_id or ""):
        raise BizError(40001, "upload_id 不合法", http_status=400)
    return settings.chunk_dir / upload_id


async def _cleanup_stale_chunks(keep_id: Optional[str] = None) -> None:
    """清理超过 UPLOAD_CHUNK_STALE_HOURS 未完成的分片目录（上传中断留下的残留）。

    过期时长从配置中心读取（可热调整），不传时用 settings 默认值。
    """
    try:
        stale_hours = int(await app_config.get_value("UPLOAD_CHUNK_STALE_HOURS"))
    except Exception:  # noqa: BLE001 —— 配置读不到不应阻断上传
        stale_hours = int(settings.UPLOAD_CHUNK_STALE_HOURS)
    cutoff = time.time() - stale_hours * 3600
    for item in settings.chunk_dir.iterdir():
        try:
            if not item.is_dir() or item.name == keep_id:
                continue
            if item.stat().st_mtime < cutoff:
                shutil.rmtree(item, ignore_errors=True)
                logger.info("清理过期分片目录：%s", item.name)
        except OSError:  # 目录恰好被并发的另一请求删除，忽略
            continue


class CompleteUploadRequest(BaseModel):
    upload_id: str = Field(..., description="分块上传会话 id（UUID）")
    file_name: str = Field(..., max_length=255, description="原始文件名，用于后缀校验与入库展示名")
    total_chunks: int = Field(..., ge=1, le=MAX_TOTAL_CHUNKS)


@router.post("/upload/chunk")
async def upload_chunk(
    upload_id: str = Form(...),
    chunk_index: int = Form(..., ge=0),
    total_chunks: int = Form(..., ge=1, le=MAX_TOTAL_CHUNKS),
    file: UploadFile = File(...),
    _: dict = Depends(require_admin),
):
    """POST /api/docs/upload/chunk —— 上传单个分片（multipart）。

    分片按 `{upload_id}/{chunk_index:05d}.part` 落盘，先写 .tmp 再原子替换，
    避免并发写入读到半截文件。chunk_index=0 视为新一轮上传：清空同 id 旧分片并顺手清理过期目录。
    """
    if chunk_index >= total_chunks:
        raise BizError(40001, "chunk_index 超出 total_chunks 范围", http_status=400)

    folder = _chunk_folder(upload_id)
    if chunk_index == 0:
        await _cleanup_stale_chunks(keep_id=upload_id)
        shutil.rmtree(folder, ignore_errors=True)  # 同 id 重传时从第 0 片重新开始
    folder.mkdir(parents=True, exist_ok=True)

    # 单片上限来自配置中心（可热调整），改完立即对新请求生效
    chunk_max_mb = int(await app_config.get_value("UPLOAD_CHUNK_MAX_MB"))
    content = await _read_capped(file, chunk_max_mb * 1024 * 1024, chunk_max_mb)
    part = folder / f"{chunk_index:05d}.part"
    tmp = part.with_suffix(".tmp")
    tmp.write_bytes(content)
    tmp.replace(part)  # 原子落盘：要么是完整分片，要么不存在

    received = sum(1 for p in folder.glob("*.part"))
    logger.info(
        "分块上传 upload_id=%s 片 %d/%d（%d 字节），已收 %d 片",
        upload_id, chunk_index + 1, total_chunks, len(content), received,
    )
    return ok({
        "upload_id": upload_id,
        "chunk_index": chunk_index,
        "received": received,
        "total_chunks": total_chunks,
    })


@router.post("/upload/complete")
async def upload_complete(payload: CompleteUploadRequest, user: dict = Depends(require_admin)):
    """POST /api/docs/upload/complete —— 校验分片齐全后按序合并，走与单文件上传相同的入库链路。"""
    folder = _chunk_folder(payload.upload_id)
    if not folder.is_dir():
        raise BizError(40001, "上传会话不存在或已过期，请重新上传", http_status=400)

    parts = [folder / f"{i:05d}.part" for i in range(payload.total_chunks)]
    missing = [p.name for p in parts if not p.is_file()]
    if missing:
        raise BizError(
            40001,
            f"分片不完整：缺少 {len(missing)}/{payload.total_chunks} 片，请检查网络后重传",
            http_status=400,
        )

    # 合并前先按分片大小求和做总大小校验，超限时不必把内容拼进内存
    total_size = sum(p.stat().st_size for p in parts)
    total_max_mb = int(await app_config.get_value("UPLOAD_TOTAL_MAX_MB"))
    _validate_upload(payload.file_name, total_size, max_bytes=total_max_mb * 1024 * 1024)

    try:
        buf = bytearray()
        for part in parts:
            buf.extend(part.read_bytes())
        result = await ingest.process(payload.file_name, bytes(buf))
    finally:
        # 无论入库成功与否都清掉临时分片，避免占盘
        shutil.rmtree(folder, ignore_errors=True)

    await audit.log_operation(
        user["user_id"], audit.DOC_UPLOAD, "document", result["doc_id"], 1,
        {
            "file_name": payload.file_name,
            "chunk_count": result["chunk_count"],
            "upload_mode": "chunked",
            "total_chunks": payload.total_chunks,
            "file_size": total_size,
        },
    )
    logger.info(
        "分块上传完成 %s：%d 片 / %d 字节 → doc_id=%s，%d 个切片",
        payload.file_name, payload.total_chunks, total_size,
        result["doc_id"], result["chunk_count"],
    )
    return ok(result)


@router.delete("/upload/chunk")
async def abort_chunk_upload(
    upload_id: str = Query(..., description="要放弃的分块上传会话 id"),
    _: dict = Depends(require_admin),
):
    """DELETE /api/docs/upload/chunk?upload_id= —— 放弃上传并清理已落盘分片。"""
    folder = _chunk_folder(upload_id)
    existed = folder.is_dir()
    shutil.rmtree(folder, ignore_errors=True)
    return ok({"upload_id": upload_id, "cleaned": existed})


@router.post("/{doc_id}/reparse")
async def reparse_doc(doc_id: int, user: dict = Depends(require_admin)):
    """POST /api/docs/{doc_id}/reparse —— 管理员重读原件、重切片、重向量化并事务替换切片。"""
    result = await maintenance.reparse_document(doc_id)
    await audit.log_operation(
        user["user_id"], audit.REPARSE, "document", doc_id, 1,
        {"old_chunks": result.get("old_chunks"), "new_chunks": result.get("new_chunks")},
    )
    return ok(result)


@router.post("/reindex")
async def rebuild_vector_index(user: dict = Depends(require_admin)):
    """POST /api/docs/reindex —— 管理员重建 HNSW 向量索引（纯 DDL，不改变文档内容）。"""
    result = await maintenance.rebuild_vector_index()
    await audit.log_operation(user["user_id"], audit.REINDEX, "index", "idx_chunk_embedding", 1)
    return ok(result)


@router.get("/list")
async def list_docs(
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=100),
    keyword: str = Query("", description="按文档名模糊筛选"),
    status: Optional[int] = Query(None, ge=0, le=2, description="0 处理中 / 1 成功 / 2 失败"),
    start_date: str = Query("", description="上传时间下界 YYYY-MM-DD，包含当天"),
    end_date: str = Query("", description="上传时间上界 YYYY-MM-DD，包含当天"),
    _: dict = Depends(require_admin),
):
    """GET /api/docs/list?page=&size=&keyword=&status=&start_date=&end_date= —— 管理员分页读取文档列表。"""
    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")
    if start and end and start > end:
        raise BizError(40001, "开始日期不能晚于结束日期", http_status=400)
    total, items = await vs.list_documents(
        page,
        size,
        keyword=keyword or None,
        status=status,
        start_date=start,
        end_date=end,
    )
    return ok({"total": total, "list": items})


def _parse_date(value: str, field: str) -> Optional[date]:
    """把 YYYY-MM-DD 解析为 date；空串返回 None，非法格式返回 40001。"""
    if not value or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise BizError(40001, f"{field} 必须是 YYYY-MM-DD 格式", http_status=400) from exc


@router.get("/{doc_id}/preview")
async def preview_doc(doc_id: int, user: dict = Depends(get_current_user)):
    """GET /api/docs/{doc_id}/preview —— 已登录用户读取原件预览。

    PDF 直接 inline 返回原文件；其他格式返回 JSON 可读内容（Markdown/文本/表格）。
    预览只读，不触发重解析、切片或向量化。
    """
    meta = await vs.get_document_meta(doc_id)
    if meta is None:
        raise BizError(ERR_DOC_NOT_FOUND, "文档不存在", http_status=404)
    if meta.get("status") == 0:
        raise BizError(40001, "文档处理中，暂不可预览", http_status=400)

    path = preview_service.resolve_original(meta)
    ext = Path(str(meta.get("doc_name") or "")).suffix.lower()
    if ext == ".pdf":
        return FileResponse(
            path,
            media_type="application/pdf",
            filename=preview_service.safe_download_name(meta.get("doc_name"), ext),
            content_disposition_type="inline",
        )

    data = await run_in_threadpool(preview_service.build_preview, meta)
    return ok(data)


@router.get("/{doc_id}/download")
async def download_doc(doc_id: int, user: dict = Depends(require_admin)):
    """GET /api/docs/{doc_id}/download —— 仅管理员下载原件，写下载审计。"""
    meta = await vs.get_document_meta(doc_id)
    if meta is None:
        raise BizError(ERR_DOC_NOT_FOUND, "文档不存在", http_status=404)
    path = preview_service.resolve_original(meta)
    ext = Path(str(meta.get("doc_name") or "")).suffix.lower()
    size = path.stat().st_size
    await audit.log_operation(
        user["user_id"], audit.DOC_DOWNLOAD, "document", doc_id, 1,
        {"file_name": meta.get("doc_name"), "bytes": size},
    )
    return FileResponse(
        path,
        media_type=preview_service.guess_media_type(ext),
        filename=preview_service.safe_download_name(meta.get("doc_name"), ext),
        content_disposition_type="attachment",
    )


@router.delete("/{doc_id}")
async def delete_doc(doc_id: int, user: dict = Depends(require_admin)):
    """DELETE /api/docs/{doc_id} —— 管理员单事务删除切片与文档；返回 {"deleted_chunks": n}。"""
    deleted_chunks = await vs.delete_document(doc_id)
    await audit.log_operation(
        user["user_id"], audit.DOC_DELETE, "document", doc_id, 1,
        {"deleted_chunks": deleted_chunks},
    )
    return ok({"deleted_chunks": deleted_chunks})
