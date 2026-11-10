"""文档知识库管理员路由。

- POST /api/docs/upload：后缀白名单 + 大小校验后，交 services.ingest.process 完成 解析 → 清洗 → 切片 → 向量化 → T1/T2/T3 事务入库；
- POST /api/docs/upload/batch：服务端顺序处理多文件，聚合 success/failed；
- GET  /api/docs/list：分页文档列表，管理员专属；
- DELETE /api/docs/{doc_id}：单事务删除切片与文档；
- POST /api/docs/{doc_id}/reparse：后端运维能力，重读原件并事务替换切片；
- POST /api/docs/reindex：后端运维能力，重建 HNSW 向量索引。
权限：全部写接口与文档列表均 require_admin；每次写操作落 operation_log。
SQL 全部收敛在 services/vector_store.py，路由不拼 SQL。
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, UploadFile, File, Query

from errors import BizError, ok, ERR_INGEST_FAILED
from config import settings
from services import audit, ingest, vector_store as vs, maintenance
from services.auth_deps import require_admin

logger = logging.getLogger("rag.docs")

router = APIRouter(prefix="/api/docs", tags=["docs"])

ALLOWED_EXTS = {".pdf", ".docx", ".md"}


def _validate_upload(filename: str, size: int) -> None:
    """校验文件后缀白名单与大小；校验失败直接拒绝，不落盘、不入库。"""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise BizError(40002, f"仅支持 {', '.join(sorted(ALLOWED_EXTS))} 文件", http_status=400)
    if size > settings.upload_max_bytes:
        raise BizError(
            40003,
            f"文件超过 {settings.UPLOAD_MAX_MB}MB 限制，请拆分后上传",
            http_status=400,
        )


async def _process_single_upload(file: UploadFile) -> dict:
    """单文件统一处理：读取 → 安全校验 → 入库；供单文件与批量端点复用。"""
    name = file.filename or "未命名文档"
    content = await file.read()
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
    _: dict = Depends(require_admin),
):
    """GET /api/docs/list?page=&size= —— 管理员分页读取文档列表。"""
    total, items = await vs.list_documents(page, size)
    return ok({"total": total, "list": items})


@router.delete("/{doc_id}")
async def delete_doc(doc_id: int, user: dict = Depends(require_admin)):
    """DELETE /api/docs/{doc_id} —— 管理员单事务删除切片与文档；返回 {"deleted_chunks": n}。"""
    deleted_chunks = await vs.delete_document(doc_id)
    await audit.log_operation(
        user["user_id"], audit.DOC_DELETE, "document", doc_id, 1,
        {"deleted_chunks": deleted_chunks},
    )
    return ok({"deleted_chunks": deleted_chunks})
