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
import importlib.util
import os
import selectors
import sys
from pathlib import Path

# 保证无论从哪个目录启动，都能 import 到 backend 包（入口为 backend.main:app）
BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 兼容有人误用系统 Python 启动：自动切回项目 .venv，避免“上传 Excel 才报缺少 openpyxl”
_VENV_PYTHON = PROJECT_ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _ensure_project_python() -> None:
    if os.environ.get("RAG_VENV_REEXEC") == "1" or not _VENV_PYTHON.exists():
        return
    try:
        current = Path(sys.executable).resolve()
        target = _VENV_PYTHON.resolve()
    except OSError:
        return
    if current == target:
        return
    print(f"[run_dev] 当前 Python 不是项目虚拟环境，自动切换：{target}", flush=True)
    env = dict(os.environ)
    env["RAG_VENV_REEXEC"] = "1"
    os.execve(str(target), [str(target), str(Path(__file__).resolve()), *sys.argv[1:]], env)


def _check_ingest_dependencies() -> None:
    """启动即校验文档解析依赖，缺依赖直接给出安装命令，避免上传时才失败。"""
    modules = {
        "openpyxl": "openpyxl",
        "xlrd": "xlrd",
        "docx": "python-docx",
        "mammoth": "mammoth",
    }
    missing = [pkg for module, pkg in modules.items() if importlib.util.find_spec(module) is None]
    if not missing:
        return
    print(
        "[run_dev] 缺少文档解析依赖：" + ", ".join(missing) + "\n"
        f"[run_dev] 请执行：{sys.executable} -m pip install -r {PROJECT_ROOT / 'backend' / 'requirements.txt'}",
        file=sys.stderr,
        flush=True,
    )
    raise SystemExit(2)


_ensure_project_python()
_check_ingest_dependencies()

from uvicorn import Config, Server  # noqa: E402

from backend.config import settings  # noqa: E402


def main() -> None:
    config = Config(
        "backend.main:app",
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
