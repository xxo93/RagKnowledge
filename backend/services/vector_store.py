"""知识库仓储：文档表 + 切片向量表的唯一 SQL 出口（架构 ADR-002 / R2）。

硬约束：
- 全项目所有 SQL 集中在本文件与 db.py；services / agent / routers 不得自行拼 SQL（R2）；
- 100% 参数化（%(name)s 占位），统计维度走枚举映射常量，杜绝拼接（§8.4）；
- 向量入参统一经 to_vector() 包装为 pgvector.Vector（ADR-006，list 直传会报类型错误）；
- 事务语义对应架构 §5.3：T1 建记录、T2 原子入库、T3 失败标记、删除文档显式事务。

骨架说明：SQL 已按需求/设计书写；数据库未就绪时 get_pool() 统一抛 50001，
真实联调在 M0（建库建表）与 M1（入库链路）完成。
"""
from __future__ import annotations

import logging
from typing import Optional

from db import get_pool, to_vector
from config import settings
from errors import BizError, ERR_DB_UNAVAILABLE, ERR_DOC_NOT_FOUND

logger = logging.getLogger("rag.vector_store")

# doc_stats 枚举白名单 -> 常量 SQL（§4.4 / §4.5：绝不拼接用户输入）
_STAT_SQL = {
    "total_docs": "SELECT count(*) FROM document",
    "success_docs": "SELECT count(*) FROM document WHERE status = 1",
    "failed_docs": "SELECT count(*) FROM document WHERE status = 2",
    "total_chunks": "SELECT count(*) FROM document_chunk",
}


# ---------- T1：插入文档元记录（status=0），立即提交以拿到 doc_id ----------
async def insert_document_meta(doc_name: str, file_path: str, file_size: int) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO document (doc_name, file_path, file_size, status)
                    VALUES (%(name)s, %(path)s, %(size)s, 0)
                    RETURNING doc_id
                    """,
                    {"name": doc_name, "path": file_path, "size": file_size},
                )
                row = await cur.fetchone()
                return int(row[0])


# ---------- T2：切片批量写入 + 文档置成功，单事务原子提交 ----------
async def commit_ingest(doc_id: int, rows: list[dict], chunk_count: int) -> None:
    """rows: [{"chunk_index": int, "content": str, "embedding": list[float]}]"""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                for r in rows:
                    await cur.execute(
                        """
                        INSERT INTO document_chunk (doc_id, chunk_index, content, embedding)
                        VALUES (%(doc_id)s, %(idx)s, %(content)s, %(emb)s)
                        """,
                        {
                            "doc_id": doc_id,
                            "idx": r["chunk_index"],
                            "content": r["content"],
                            "emb": to_vector(r["embedding"]),
                        },
                    )
                await cur.execute(
                    "UPDATE document SET status = 1, chunk_count = %(n)s WHERE doc_id = %(id)s",
                    {"n": chunk_count, "id": doc_id},
                )


# ---------- T3：失败置 status=2 并记录原因（独立提交，不被 T2 回滚带走） ----------
async def mark_document_failed(doc_id: int, error_msg: str) -> None:
    pool = get_pool()
    msg = error_msg[:512]
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE document SET status = 2, error_msg = %(msg)s WHERE doc_id = %(id)s",
                    {"msg": msg, "id": doc_id},
                )


# ---------- 删除文档：单事务先删切片再删文档（F-03 / ADR-007 显式删除） ----------
async def delete_document(doc_id: int) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM document_chunk WHERE doc_id = %(id)s", {"id": doc_id}
                )
                deleted_chunks = cur.rowcount or 0
                await cur.execute(
                    "DELETE FROM document WHERE doc_id = %(id)s", {"id": doc_id}
                )
                if cur.rowcount == 0:
                    raise BizError(ERR_DOC_NOT_FOUND, "文档不存在或已删除", http_status=404)
                return int(deleted_chunks)


# ---------- 运维：按 doc_id 读取元数据（重解析前定位本地原件；list_documents 不返回 file_path） ----------
async def get_document_meta(doc_id: int) -> Optional[dict]:
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT doc_id, doc_name, file_path, status, chunk_count
                FROM document WHERE doc_id = %(id)s
                """,
                {"id": doc_id},
            )
            row = await cur.fetchone()
            if row is None:
                return None
            return dict(zip(
                ["doc_id", "doc_name", "file_path", "status", "chunk_count"], row
            ))


# ---------- 运维：重解析后事务替换切片（先删旧切片、再写新切片、更新计数，保留 doc_id 与元数据） ----------
async def replace_document_chunks(doc_id: int, rows: list[dict], chunk_count: int) -> None:
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM document_chunk WHERE doc_id = %(id)s", {"id": doc_id}
                )
                for r in rows:
                    await cur.execute(
                        """
                        INSERT INTO document_chunk (doc_id, chunk_index, content, embedding)
                        VALUES (%(doc_id)s, %(idx)s, %(content)s, %(emb)s)
                        """,
                        {
                            "doc_id": doc_id,
                            "idx": r["chunk_index"],
                            "content": r["content"],
                            "emb": to_vector(r["embedding"]),
                        },
                    )
                await cur.execute(
                    "UPDATE document SET status = 1, chunk_count = %(n)s WHERE doc_id = %(id)s",
                    {"n": chunk_count, "id": doc_id},
                )


# ---------- 运维：重建 HNSW 向量索引（REINDEX 不允许在事务块内执行，使用一次性 autocommit 短连接） ----------
async def reindex_vector_index() -> None:
    import psycopg

    conn = await psycopg.AsyncConnection.connect(settings.DB_DSN, autocommit=True)
    try:
        async with conn.cursor() as cur:
            await cur.execute("REINDEX INDEX idx_chunk_embedding")
    finally:
        await conn.close()


# ---------- 文档分页列表（F-02；chunk_count 为冗余字段，避免 JOIN count） ----------
async def list_documents(page: int, size: int) -> tuple[int, list[dict]]:
    pool = get_pool()
    offset = (page - 1) * size
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM document")
            total = int((await cur.fetchone())[0])
            await cur.execute(
                """
                SELECT doc_id, doc_name, file_size, chunk_count, status, error_msg, create_time
                FROM document
                ORDER BY doc_id DESC
                LIMIT %(size)s OFFSET %(offset)s
                """,
                {"size": size, "offset": offset},
            )
            cols = [c.name for c in cur.description]
            items = [dict(zip(cols, row)) for row in await cur.fetchall()]
            return total, items


# ---------- 向量检索（F-07：余弦距离 <=>，HNSW vector_cosine_ops） ----------
async def search(
    query_vec: list[float],
    top_k: int,
    doc_ids: Optional[list[int]] = None,
) -> list[dict]:
    pool = get_pool()
    qvec = to_vector(query_vec)
    async with pool.connection() as conn:
        # GUC 调节各自放在“可回滚的独立事务”里：低版本/扩展不支持时异常自动 rollback，
        # 绝不污染下方主检索事务（否则 psycopg 把事务标记 aborted，后续查询全报 InFailedSqlTransaction）
        await _set_local_guc(conn, "hnsw.iterative_scan", settings.HNSW_ITERATIVE_SCAN, "iterative_scan")
        await _set_local_guc(conn, "hnsw.ef_search", settings.HNSW_EF_SEARCH, "ef_search")

        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT c.chunk_id, c.doc_id, d.doc_name, c.chunk_index, c.content,
                           1 - (c.embedding <=> %(qvec)s) AS score
                    FROM document_chunk c
                    JOIN document d ON d.doc_id = c.doc_id
                    WHERE (%(doc_ids)s::bigint[] IS NULL OR c.doc_id = ANY(%(doc_ids)s::bigint[]))
                    ORDER BY c.embedding <=> %(qvec)s
                    LIMIT %(k)s
                    """,
                    {"qvec": qvec, "doc_ids": doc_ids, "k": top_k},
                )
                cols = [c.name for c in cur.description]
                return [dict(zip(cols, row)) for row in await cur.fetchall()]


async def _set_local_guc(conn, name: str, value: int | str, label: str) -> None:
    """以 LOCAL 方式设置检索相关 GUC；失败仅告警并回滚该独立事务，不影响主检索查询。"""
    try:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute("SELECT set_config(%s, %s, true)", (name, str(value)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("检索 GUC %s 不可设置（%s），按默认值继续", label, exc)


# ---------- 统计工具：枚举白名单映射（doc_stats） ----------
async def count_stat(stat_type: str) -> int:
    if stat_type not in _STAT_SQL:
        raise BizError(40001, f"不支持的统计类型：{stat_type}", http_status=400)
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_STAT_SQL[stat_type])
            return int((await cur.fetchone())[0])
