"""知识库运维能力。

- reparse_document：重读已有文档本地原件 -> 重新清洗/切片/向量化 -> 事务替换该文档切片，
  保留 doc_id、文档元数据与本地落盘原件；用于切片策略或 Embedding 模型调整后的存量向量刷新。
- rebuild_vector_index：对 HNSW 向量索引执行 REINDEX（纯 DDL，不产生 Embedding/Rerank 费用）。

边界：本模块不删除文档、不修改元数据；重解析失败不覆盖旧切片（replace 在同一事务提交）。
"""
from __future__ import annotations

from pathlib import Path

from fastapi.concurrency import run_in_threadpool

from config import ROOT_DIR, settings
from errors import BizError, ERR_DOC_NOT_FOUND, ERR_INGEST_FAILED
from services import cleaner, embedder as embed_mod, loader, splitter, vector_store as vs


def _build_rows_sync(abs_path: Path) -> tuple[list[dict], int]:
    """同步阻塞段：解析 -> 清洗 -> 切片 -> 向量化；由调用方放入线程池执行。"""
    raw_text = loader.extract_text(abs_path)
    text = cleaner.clean_text(raw_text)
    chunks = splitter.split_text(text, settings.CHUNK_SIZE, settings.CHUNK_OVERLAP)
    if not chunks:
        raise BizError(
            ERR_INGEST_FAILED, "重解析后无可提取文本（可能为空文档或扫描件）", http_status=500
        )
    emb = embed_mod.get_embedder()
    vectors = emb.embed_documents(chunks)
    rows = [
        {"chunk_index": idx, "content": content, "embedding": vector}
        for idx, (content, vector) in enumerate(zip(chunks, vectors))
    ]
    return rows, len(chunks)


async def reparse_document(doc_id: int) -> dict:
    """重新解析指定文档并替换切片向量；返回新旧切片数等审计信息。"""
    meta = await vs.get_document_meta(doc_id)
    if meta is None:
        raise BizError(ERR_DOC_NOT_FOUND, "文档不存在，无法重解析", http_status=404)
    if not meta.get("file_path"):
        raise BizError(ERR_INGEST_FAILED, "文档缺少本地文件路径，无法重解析", http_status=500)

    abs_path = ROOT_DIR / meta["file_path"]
    if not abs_path.exists():
        raise BizError(
            ERR_INGEST_FAILED, f"本地原件不存在：{meta['file_path']}", http_status=500
        )

    rows, chunk_count = await run_in_threadpool(_build_rows_sync, abs_path)
    summary = vs.summarize_chunks(rows)
    await vs.replace_document_chunks(doc_id, rows, chunk_count, summary=summary)
    return {
        "doc_id": doc_id,
        "doc_name": meta["doc_name"],
        "old_chunks": meta["chunk_count"],
        "new_chunks": chunk_count,
    }


async def rebuild_vector_index() -> dict:
    """重建 HNSW 向量索引；纯 DDL、不改变文档内容。"""
    await vs.reindex_vector_index()
    return {"index": "idx_chunk_embedding", "status": "reindexed"}
