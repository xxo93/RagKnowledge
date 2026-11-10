"""M1 真实联网入库闭环（M1 收尾，使用真实 ApiEmbedder / text-embedding-v3，不打补丁）。

覆盖（账户 EMBEDDING_API_KEY 可用时运行）：
1. 真实 Markdown 走 T1→解析/清洗/切片→真实批量向量化→T2，返回 status=1、chunk_count>0；
2. 回归点①：document.chunk_count 与 document_chunk 实际行数一致、列表可见 status=1；
3. 向量健全性：真实 query 向量经余弦 `<=>` 检索能命中本次写入的切片（只读，非 M2 业务逻辑）；
4. 回归点③：删除后 deleted_chunks==chunk_count、残留切片=0、记录移除。

运行前确认账户可调用（先通过 scripts/check_embedder.py 得到 EMBEDDER_OK）。
运行（cwd=backend）：..\\.venv\\Scripts\\python.exe scripts\\check_ingest_real.py
退出码：0=全部通过；1=失败；2=数据库未就绪跳过。测试 DB 记录在脚本内自动删除。
"""
from __future__ import annotations

import asyncio
import selectors
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from errors import BizError  # noqa: E402
from services import ingest, vector_store as vs, embedder as em  # noqa: E402


async def _chunk_count_of(doc_id: int) -> int:
    pool = db.get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM document_chunk WHERE doc_id = %s", (doc_id,)
            )
            return int((await cur.fetchone())[0])


async def main() -> int:
    if not await db.init_pool():
        print("SKIP_DB：数据库未就绪，请先完成 M0")
        return 2

    results: list[tuple[str, bool]] = []
    doc_id: int | None = None
    try:
        paragraph = (
            "RAG 检索增强生成系统先把用户问题向量化，再在向量库中做余弦相似度召回，"
            "取 Top-K 相关切片拼装进提示词，最后由大模型结合检索到的来源生成答案并标注引用出处。"
        )
        md = (
            "# RAG 知识库演示文档\n\n"
            + paragraph * 3
            + "\n\n系统采用 PostgreSQL 搭配 pgvector，向量列建立 HNSW 余弦索引（vector_cosine_ops）；"
            "入库链路依次为文档解析、文本清洗、重叠窗口切片、批量向量化与三事务写入。"
            "\n\n安全计算器与文档规模统计是 Agent 另外两类工具，分别处理算术求值与库内统计查询。\n"
        )

        out = await ingest.process("m1_real_demo.md", md.encode("utf-8"))
        doc_id = out["doc_id"]
        n = out["chunk_count"]

        results.append((f"真实入库返回 status={out['status']} chunk_count={n}", out["status"] == 1 and n > 0))

        real = await _chunk_count_of(doc_id)
        results.append((f"回归点① chunk_count({n}) == 切片表行数({real})", real == n))

        _, items = await vs.list_documents(1, 50)
        row = next((it for it in items if it["doc_id"] == doc_id), None)
        results.append(
            (
                "列表可见 status=1 且计数一致",
                row is not None and row["status"] == 1 and row["chunk_count"] == n,
            )
        )

        # 向量健全性：真实 query 向量可被 PG 余弦算子比较并命中本次切片（只读检查）
        qvec = em.get_embedder().embed_query("什么是 RAG 检索增强生成，入库链路包含哪些步骤")
        hits = await vs.search(qvec, 3)
        mine = [h for h in hits if h["doc_id"] == doc_id]
        results.append(
            (f"真实向量余弦可检索：返回 {len(hits)} 片、本库命中 {len(mine)} 片、含 score 字段",
             bool(mine) and all("score" in h for h in hits))
        )

        # 回归点③：删除归零
        deleted = await vs.delete_document(doc_id)
        remain = await _chunk_count_of(doc_id)
        results.append(
            (f"回归点③ 删除 deleted_chunks={deleted} 残留切片={remain}", deleted == n and remain == 0)
        )
        doc_id = None
    except BizError as exc:
        results.append((f"真实入库抛业务错误 code={exc.code} msg={exc.msg}", False))
    finally:
        if doc_id is not None:  # 异常路径兜底清理测试记录
            try:
                await vs.delete_document(doc_id)
            except Exception:  # noqa: BLE001
                pass
        await db.close_pool()

    for name, ok in results:
        print(("PASS" if ok else "FAIL"), "-", name)
    passed = all(ok for _, ok in results)
    print("REAL_INGEST_RESULT =", "ALL_PASS" if passed else "HAS_FAIL")
    return 0 if passed else 1


def _selector_factory() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop(selectors.DefaultSelector())


if __name__ == "__main__":
    sys.exit(asyncio.run(main(), loop_factory=_selector_factory))
