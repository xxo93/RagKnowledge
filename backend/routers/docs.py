"""文档知识库路由（需求 §7.1 / F-01~F-03）。

骨架阶段：路由路径与入参契约先固定；安全校验（后缀白名单、大小限制）先行落地，
解析-切片-向量化-入库链路在 M1 经 services.ingest 接通。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, UploadFile, File, Query

from errors import BizError, ok
from config import settings

router = APIRouter(prefix="/api/docs", tags=["docs"])

ALLOWED_EXTS = {".pdf", ".docx", ".md"}


def _validate_upload(filename: str, size: int) -> None:
    """安全前置校验：后缀白名单 + 大小限制（F-01 / §8.4），非法直接拒绝、不落盘。"""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise BizError(40002, f"仅支持 {', '.join(sorted(ALLOWED_EXTS))} 文件", http_status=400)
    if size > settings.upload_max_bytes:
        raise BizError(
            40003,
            f"文件超过 {settings.UPLOAD_MAX_MB}MB 限制，请拆分后上传",
            http_status=400,
        )


@router.post("/upload")
async def upload_doc(file: UploadFile = File(...)):
    """POST /api/docs/upload —— 同步处理并入库（M1 接通 ingest.process）。"""
    content = await file.read()
    _validate_upload(file.filename or "", len(content))
    # TODO(M1): 调用 services.ingest.process(file.filename, content) 完成
    #           解析 -> 清洗 -> 切片 -> 向量化 -> T1/T2/T3 事务入库
    raise BizError(50100, "上传处理链路将在 M1 阶段接通（当前已完成后缀与大小校验）", http_status=501)


@router.get("/list")
async def list_docs(page: int = Query(1, ge=1), size: int = Query(10, ge=1, le=100)):
    """GET /api/docs/list?page=&size= —— 分页文档列表（F-02，M1 接通）。"""
    raise BizError(50100, "文档列表将在 M1 阶段接通", http_status=501)


@router.delete("/{doc_id}")
async def delete_doc(doc_id: int):
    """DELETE /api/docs/{doc_id} —— 单事务删除切片与文档（F-03，M1 接通）。"""
    raise BizError(50100, "文档删除将在 M1 阶段接通", http_status=501)
