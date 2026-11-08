"""入库流水线编排与事务边界（需求 F-01 / 架构 §3.1，T1/T2/T3 三事务设计）。

事务划分（为什么不是一个大事务，见架构 §3.1）：
- T1 insert_document_meta：先落 status=0 行并拿到 doc_id，立即提交；
- T2 commit_ingest：批量写切片向量 + 置成功，原子提交，失败整体回滚；
- T3 mark_document_failed：任一环节失败时置 status=2 + error_msg（独立提交，避免被回滚带走）。
解析与 Embedding 期间不持有数据库连接（外部调用耗时不可控，防止长事务占连接）。
"""
from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path

from config import settings
from errors import BizError, ERR_INGEST_FAILED
from services import loader, cleaner, splitter, embedder as embed_mod, vector_store as vs

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


async def process(original_name: str, file_bytes: bytes) -> dict:
    """上传同步处理主流程（F-01）。骨架阶段为编排骨架，M1 逐步接通。

    返回 {"doc_id", "chunk_count", "status"}；失败统一落 status=2 并抛业务错误给路由层。
    """
    # 0. 后缀 / 大小前置校验在 routers/docs.py 完成；这里进入处理编排
    rel_path, abs_path = safe_storage_name(original_name)
    ext = Path(original_name).suffix.lower()

    # T1：落文档记录（status=0）
    doc_id = await vs.insert_document_meta(
        doc_name=Path(original_name).name,
        file_path=rel_path,
        file_size=len(file_bytes),
    )

    try:
        # 1) 保存原文件到本地（原始留档，§5.6）
        abs_path.write_bytes(file_bytes)

        # 2) 解析 -> 清洗 -> 切片（M1 接通真实库；当前 loader 已具备分派契约）
        raw_text = loader.extract_text(abs_path)
        text = cleaner.clean_text(raw_text)
        chunks = splitter.split_text(text, settings.CHUNK_SIZE, settings.CHUNK_OVERLAP)
        if not chunks:
            raise BizError(ERR_INGEST_FAILED, "文档无可提取文本（可能为扫描件或加密文件）", http_status=500)

        # 3) 批量向量化（同模型同维度；维度自检在 embedder 内执行）
        emb = embed_mod.get_embedder()
        vectors = emb.embed_documents(chunks)  # TODO(M1): 接通真实实现 + 分批 + 重试
        rows = [
            {"chunk_index": idx, "content": content, "embedding": vec}
            for idx, (content, vec) in enumerate(zip(chunks, vectors))
        ]

        # T2：原子写入切片并置成功
        await vs.commit_ingest(doc_id, rows, len(chunks))
        logger.info("入库成功 doc_id=%d chunks=%d", doc_id, len(chunks))
        return {"doc_id": doc_id, "chunk_count": len(chunks), "status": 1}

    except Exception as exc:  # noqa: BLE001
        # T3：失败标记（独立提交）；同时清理残留空切片由 T2 回滚保证
        reason = getattr(exc, "msg", str(exc)) or "文档处理失败"
        try:
            await vs.mark_document_failed(doc_id, reason)
        except Exception:  # noqa: BLE001
            logger.exception("失败状态写入异常 doc_id=%d", doc_id)
        logger.error("入库失败 doc_id=%d：%s", doc_id, reason)
        raise BizError(ERR_INGEST_FAILED, "文档处理失败，请查看列表状态与错误提示", http_status=500) from exc
