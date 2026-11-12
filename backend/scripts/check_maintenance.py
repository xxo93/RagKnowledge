"""运维能力端到端临时验证：文档重解析替换 + HNSW 向量索引 REINDEX。

纪律：
- 只创建一个 `maint_temp.md` 临时文档验证；不修改、不重解析留存 4 篇样料；
- 重解析走真实 Embedding（少量费用），REINDEX 为纯 DDL、无模型费用；
- finally 删除临时文档 DB 记录（级联切片）并 unlink 本地落盘文件；权威基线保持 (4,4,0,0)。

运行（cwd=backend；数据库就绪）：
    ..\\.venv\\Scripts\\python.exe scripts\\check_maintenance.py
退出码：0=验证成功且清理完成；1=异常；2=数据库未就绪。
"""
from __future__ import annotations

import asyncio
import selectors
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import backend.db  # noqa: E402
from backend.config import ROOT_DIR  # noqa: E402
from backend.services import ingest, maintenance, vector_store as vs  # noqa: E402

TEMP_NAME = "maint_temp.md"
TEMP_TEXT = (
    "运维临时测试文档。本文件仅用于验证文档重解析与 HNSW 索引重建能力；"
    "内含唯一可定位事实：内部安全巡检周期为每两周一次，巡检结果需在安全台账中留痕。"
)


async def main() -> int:
    if not await db.init_pool():
        print("SKIP_DB：数据库未就绪，无法验证运维能力")
        return 2

    created: list[int] = []
    verified = False
    try:
        out = await ingest.process(TEMP_NAME, TEMP_TEXT.encode("utf-8"))
        doc_id = out["doc_id"]
        created.append(doc_id)
        before = await vs.get_document_meta(doc_id)
        print(f"[INGEST] doc_id={doc_id} status={before['status']} chunks={before['chunk_count']}")

        reparse = await maintenance.reparse_document(doc_id)
        print(f"[REPARSE] {reparse}")
        assert reparse["doc_id"] == doc_id
        after = await vs.get_document_meta(doc_id)
        assert after["status"] == 1, "重解析后文档状态应为成功"
        assert after["chunk_count"] == reparse["new_chunks"], "切片计数应与重解析结果一致"

        rebuilt = await maintenance.rebuild_vector_index()
        print(f"[REINDEX] {rebuilt}")
        verified = True
        print("MAINTENANCE_RESULT = OK")
    except Exception as exc:  # noqa: BLE001
        print(f"MAINTENANCE_EXCEPTION = {type(exc).__name__}: {exc}")
    finally:
        for doc_id in created:
            try:
                meta = await vs.get_document_meta(doc_id)
                if meta:
                    await vs.delete_document(doc_id)
                    (ROOT_DIR / meta["file_path"]).unlink(missing_ok=True)
                    print(f"[CLEANUP] 删除临时 doc_id={doc_id} 及其本地文件")
            except Exception as exc:  # noqa: BLE001
                print(f"[CLEANUP-WARN] doc_id={doc_id}: {type(exc).__name__}: {exc}")
        await db.close_pool()

    return 0 if verified else 1


def _selector_factory() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop(selectors.DefaultSelector())


if __name__ == "__main__":
    sys.exit(asyncio.run(main(), loop_factory=_selector_factory))
