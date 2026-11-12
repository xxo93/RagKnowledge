"""入库流水线编排与事务边界（T1/T2/T3 三事务设计）。

事务划分（为什么不是一个大事务）：
- T1 insert_document_meta：先落 status=0 行并拿到 doc_id，立即提交；
- T2 commit_ingest：批量写切片向量 + 置成功，原子提交，失败整体回滚；
- T3 mark_document_failed：任一环节失败时置 status=2 + error_msg（独立提交，避免被回滚带走）。

解析与 Embedding 期间不持有数据库连接（外部调用耗时不可控，防止长事务占连接）；
同步阻塞的解析/切片/向量化经 run_in_threadpool 调度，不阻塞 asyncio 事件循环。
"""
from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path

from fastapi.concurrency import run_in_threadpool

from ..config import settings
from ..errors import BizError, ERR_INGEST_FAILED
from . import loader, cleaner, splitter, embedder as embed_mod, vector_store as vs

logger = logging.getLogger("rag.ingest")


def safe_storage_name(original_name: str) -> tuple[str, Path]:
    """生成安全存储文件名并返回 (相对路径, 绝对路径)（架构 §8.4 防路径穿越）。

    - 只取基名，剔除目录与 ..；
    - 存储名 = {yyyyMMdd}/{时间戳}_{uuid8}.{ext}，原始名存 doc_name 展示；
    - 入库只存相对路径，不写绝对路径。
    """
    base = Path(original_name).name.replace("..", "").strip()
    ext = Path(base).suffix.lower()
    day = time.strftime("%Y%m%d")
    folder = settings.upload_dir / day
    folder.mkdir(parents=True, exist_ok=True)
    stored = f"{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}"
    rel_path = f"data/uploads/{day}/{stored}"
    return rel_path, folder / stored


def _build_rows(abs_path: Path) -> tuple[list[dict], int]:
    """同步重活：解析 → 清洗 → 切片 → 向量化，返回 (rows, chunk_count)。

    rows: [{"chunk_index", "content", "embedding": list[float]}]；由 run_in_threadpool 执行，
    避免 PDF/DOCX 解析与同步 HTTP 向量化阻塞事件循环。
    """
    raw_text = loader.extract_text(abs_path)
    text = cleaner.clean_text(raw_text)
    chunks = splitter.split_text(text, settings.CHUNK_SIZE, settings.CHUNK_OVERLAP)
    if not chunks:
        raise BizError(
            ERR_INGEST_FAILED, "文档无可提取文本（可能为扫描件或加密文件）", http_status=500
        )
    emb = embed_mod.get_embedder()
    vectors = emb.embed_documents(chunks)
    rows = [
        {"chunk_index": idx, "content": content, "embedding": vec}
        for idx, (content, vec) in enumerate(zip(chunks, vectors))
    ]
    return rows, len(chunks)


async def process(original_name: str, file_bytes: bytes) -> dict:
    """上传同步处理主流程（F-01）。

    返回 {"doc_id", "chunk_count", "status"}；业务错误经 T3 落 status=2 后原样抛出，
    未知异常统一包成 ERR_INGEST_FAILED(50003) 抛出给路由层。
    """
    # 0. 后缀 / 大小前置校验在 routers/docs.py 完成；这里进入处理编排
    rel_path, abs_path = safe_storage_name(original_name)

    # T1：落文档记录（status=0），立即提交以拿到 doc_id
    doc_id = await vs.insert_document_meta(
        doc_name=Path(original_name).name,
        file_path=rel_path,
        file_size=len(file_bytes),
    )

    try:
        # 1) 保存原文件到本地（原始留档，§5.6）
        abs_path.write_bytes(file_bytes)

        # 2) 解析 -> 清洗 -> 切片 -> 向量化（同步阻塞段放线程池；不占 DB 连接）
        rows, chunk_count = await run_in_threadpool(_build_rows, abs_path)

        # T2：原子写入切片并置成功；文档摘要由切片文本确定性归并
        summary = vs.summarize_chunks(rows)
        await vs.commit_ingest(doc_id, rows, chunk_count, summary=summary)
        logger.info("入库成功 doc_id=%d chunks=%d", doc_id, chunk_count)
        return {"doc_id": doc_id, "chunk_count": chunk_count, "status": 1}

    except BizError as exc:
        # 业务错误（空文本 / 维度不符 / 向量化服务失败等）：T3 落 status=2，保留原 code 与 http_status
        try:
            await vs.mark_document_failed(doc_id, exc.msg)
        except Exception:  # noqa: BLE001
            logger.exception("失败状态写入异常 doc_id=%d", doc_id)
        logger.error("入库失败 doc_id=%d：%s", doc_id, exc.msg)
        raise

    except Exception as exc:  # noqa: BLE001 - 未知异常统一兜底为入库失败业务码
        reason = str(exc) or "文档处理失败"
        try:
            await vs.mark_document_failed(doc_id, reason[:512])
        except Exception:  # noqa: BLE001
            logger.exception("失败状态写入异常 doc_id=%d", doc_id)
        logger.error("入库失败 doc_id=%d：%s", doc_id, reason)
        raise BizError(
            ERR_INGEST_FAILED, "文档处理失败，请查看列表状态与错误提示", http_status=500
        ) from exc
