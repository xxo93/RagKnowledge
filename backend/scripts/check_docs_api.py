"""只读路由层联通验证（M1 收尾，不联网、不写库）。

用途：在真实 PostgreSQL 已就绪的前提下，验证两个只读 HTTP 路由真正走到数据库 SQL，
而不仅是装配层存在：
- GET /api/docs/list?page=1&size=10  → 走 routers/docs.list_docs → vector_store.list_documents；
- GET /api/system/info               → 走 routers/system.info，回读 db_ready / 文档与切片计数。

说明：
- 不调用 Embedding、不调用 LLM、不产生任何写入；
- 不进入 FastAPI lifespan，因此在脚本内手动 init_pool()，结束后 close_pool()；
- Windows 下显式 SelectorEventLoop（psycopg 异步要求，与 run_dev.py 一致）。

运行（cwd=backend）：..\\.venv\\Scripts\\python.exe scripts\\check_docs_api.py
退出码：0=通过；1=断言失败；2=数据库未就绪跳过。
"""
from __future__ import annotations

import asyncio
import selectors
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
import main as app_module  # noqa: E402


async def main() -> int:
    if not await db.init_pool():
        print("SKIP_DB：数据库未就绪，请先用 run_dev.py / init_db.py 完成 M0")
        return 2

    results: list[tuple[str, bool]] = []
    try:
        transport = ASGITransport(app=app_module.app)
        async with AsyncClient(transport=transport, base_url="http://local") as client:
            # 1) 文档列表：真实执行 SELECT count + 分页 SELECT
            resp = await client.get("/api/docs/list", params={"page": 1, "size": 10})
            body = resp.json()
            data = body.get("data") or {}
            ok_list = (
                resp.status_code == 200
                and body.get("code") == 0
                and "total" in data
                and isinstance(data.get("list"), list)
            )
            results.append((f"GET /api/docs/list -> HTTP {resp.status_code} code={body.get('code')} total={data.get('total')} list_len={len(data.get('list', []))}", ok_list))

            # 2) 系统信息：db_ready 与只读统计量
            resp2 = await client.get("/api/system/info")
            body2 = resp2.json()
            d2 = body2.get("data") or {}
            ok_info = (
                resp2.status_code == 200
                and body2.get("code") == 0
                and d2.get("db_ready") is True
            )
            results.append((f"GET /api/system/info -> HTTP {resp2.status_code} db_ready={d2.get('db_ready')} doc_total={d2.get('doc_total')} chunk_total={d2.get('chunk_total')}", ok_info))
    finally:
        await db.close_pool()

    for name, ok in results:
        print(("PASS" if ok else "FAIL"), "-", name)
    passed = all(ok for _, ok in results)
    print("DOCS_API_RESULT =", "ALL_PASS" if passed else "HAS_FAIL")
    return 0 if passed else 1


def _selector_factory() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop(selectors.DefaultSelector())


if __name__ == "__main__":
    sys.exit(asyncio.run(main(), loop_factory=_selector_factory))
