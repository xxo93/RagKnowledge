"""M1 入库链路事务与计数回归（不依赖网络 / Embedding Key）。

覆盖架构 §1142–1144 三条回归点：
1. 成功入库：document.chunk_count 与 document_chunk 实际行数一致；
2. 失败入库：status=2、error_msg 非空、该文档切片数为 0；
3. 删除文档后：返回 deleted_chunks 且残留切片为 0。

实现说明：
- monkeypatch `embedder.get_embedder` 返回确定性 1024 维假向量，避免真实联网；
  真实 API 向量化（维度/分批）由 `check_embedder.py` 在配置 EMBEDDING_API_KEY 后单独验证；
- 前置：数据库必须就绪（M0：建库、vector 扩展、四表 + HNSW）；
- Windows 下显式使用 SelectorEventLoop，与 scripts/run_dev.py 保持一致（psycopg 异步要求）。

运行：.venv\\Scripts\\python.exe backend\\scripts\\check_ingest_chain.py
退出码：0=全部通过；1=存在失败；2=数据库未就绪跳过。
"""
from __future__ import annotations

import asyncio
import selectors
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from config import settings  # noqa: E402
from errors import BizError  # noqa: E402
from services import ingest, vector_store as vs, embedder as embed_mod  # noqa: E402


class _FakeEmbedder:
    """确定性零向量实现，仅用于本地事务/计数回归，不代表真实语义。"""

    dim = settings.EMBEDDING_DIM

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * settings.EMBEDDING_DIM for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        return [0.0] * settings.EMBEDDING_DIM


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
        print("SKIP_DB：数据库未就绪，请先完成 M0（建库 / vector 扩展 / init_db）")
        return 2

    original_factory = embed_mod.get_embedder
    embed_mod.get_embedder = lambda: _FakeEmbedder()  # 固定假向量，避免联网
    results: list[tuple[str, bool]] = []
    try:
        # 回归点 1：成功入库，chunk_count 与切片表实际行数一致
        md = "# M1 链路回归文档\n\n" + ("这是用于验证切片入库与计数一致性的中文段落。" * 40) + "\n"
        out = await ingest.process("m1_chain_success.md", md.encode("utf-8"))
        assert out["status"] == 1, "成功入库应返回 status=1"
        doc_id, n = out["doc_id"], out["chunk_count"]
        assert n > 0, "成功入库切片数应大于 0"
        real = await _chunk_count_of(doc_id)
        results.append((f"成功入库计数一致：chunk_count={n}，切片表行数={real}", real == n))

        # 回归点 3：删除文档后切片归零
        deleted = await vs.delete_document(doc_id)
        remain = await _chunk_count_of(doc_id)
        results.append(
            (f"删除后归零：deleted_chunks={deleted}，残留切片={remain}", deleted == n and remain == 0)
        )

        # 回归点 2：空文本失败 -> status=2、error_msg 非空、切片为 0
        try:
            await ingest.process("m1_chain_empty.md", b"   \n  ")
            results.append(("空文本文档应被拒绝（返回失败）", False))
        except BizError:
            _, items = await vs.list_documents(1, 50)
            failed = [it for it in items if it["status"] == 2]
            if not failed:
                results.append(("失败文档应出现在列表且 status=2", False))
            else:
                fdoc = failed[0]["doc_id"]
                fchunks = await _chunk_count_of(fdoc)
                ok2 = failed[0]["status"] == 2 and bool(failed[0]["error_msg"]) and fchunks == 0
                results.append(
                    (f"失败文档 status=2、error_msg 非空、切片=0（doc_id={fdoc}）", ok2)
                )
                await vs.delete_document(fdoc)  # 清理失败文档残留记录
    finally:
        embed_mod.get_embedder = original_factory
        await db.close_pool()

    for name, ok in results:
        print(("PASS" if ok else "FAIL"), "-", name)
    passed = all(ok for _, ok in results)
    print("CHAIN_RESULT =", "ALL_PASS" if passed else "HAS_FAIL")
    return 0 if passed else 1


def _run() -> None:
    if sys.platform == "win32":
        def _selector_factory() -> asyncio.AbstractEventLoop:
            return asyncio.SelectorEventLoop(selectors.DefaultSelector())

        asyncio.run(main(), loop_factory=_selector_factory)
    else:
        asyncio.run(main())


if __name__ == "__main__":
    _run()
