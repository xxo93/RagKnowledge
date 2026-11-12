"""历史文档摘要回填：从既有切片确定性归并 ≤120 字文档摘要。

运行（cwd=backend，.venv Python 3.12.7）：
    python scripts/backfill_doc_summary.py

说明：本脚本不调用额外 LLM；只更新 status=1 且 summary 为空的文档。
"""
from __future__ import annotations

import asyncio
import selectors
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import backend.db  # noqa: E402
from backend.services import vector_store as vs  # noqa: E402


async def main() -> int:
    await db.init_pool()
    try:
        docs = await vs.list_docs_without_summary()
        updated = 0
        for doc in docs:
            chunks = await vs.get_chunks_for_summary(doc["doc_id"])
            summary = vs.summarize_chunks(chunks)
            if summary:
                await vs.update_document_summary(doc["doc_id"], summary)
                updated += 1
        print(f"回填完成：更新 {updated}/{len(docs)} 篇历史文档摘要")
        return 0
    finally:
        await db.close_pool()


def _selector_factory() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop(selectors.SelectSelector())


if __name__ == "__main__":
    sys.exit(asyncio.run(main(), loop_factory=_selector_factory))
