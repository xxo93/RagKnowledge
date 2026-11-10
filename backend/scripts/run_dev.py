"""开发启动入口（Windows 兼容 psycopg 异步；非 Windows 透传 uvicorn）。

背景：Windows 默认 ProactorEventLoop 不被 psycopg 3 异步驱动支持，连接时报
“cannot use the 'ProactorEventLoop' to run in async mode”。仅设置默认事件循环策略
不一定被 uvicorn 采纳，因此这里用 Server.serve() + asyncio.run(loop_factory=...)
显式构造 SelectorEventLoop；非 Windows 平台不做干预。

用法（工作目录建议 backend/）：
    ..\\.venv\\Scripts\\python.exe scripts\\run_dev.py
监听主机/端口取自 config.py（APP_HOST / APP_PORT，默认 127.0.0.1:8000）。
裸 uvicorn 命令可用于“不接数据库的只读冷启动”，但 Windows 下真实异步 DB 连接请用本入口。
"""
from __future__ import annotations

import asyncio
import selectors
import sys
from pathlib import Path

# 保证无论从哪个目录启动，都能 import 到 backend 包与 main:app
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from uvicorn import Config, Server  # noqa: E402

from config import settings  # noqa: E402


def main() -> None:
    config = Config(
        "main:app",
        host=settings.APP_HOST,
        port=settings.APP_PORT,
        log_level="info",
        reload=False,
    )
    server = Server(config)

    if sys.platform == "win32":
        # Python 3.12+ 的 asyncio.run 支持 loop_factory；SelectorEventLoop 兼容 psycopg 异步
        def _selector_factory() -> asyncio.SelectorEventLoop:
            return asyncio.SelectorEventLoop(selectors.DefaultSelector())

        asyncio.run(server.serve(), loop_factory=_selector_factory)
    else:
        asyncio.run(server.serve())


if __name__ == "__main__":
    main()
