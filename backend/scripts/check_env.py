"""环境与依赖自检脚本（可重复运行）。

用途：
1. 逐个校验第三方依赖能否导入、打印版本；
2. 校验项目内部模块（如 services.rag，它不是 pip 包）能否解析；
3. 装配 FastAPI app 并打印已注册路由，确认代码无导入错误。

运行（在 backend/ 目录下，使用项目 .venv）：
    ..\\.venv\\Scripts\\python.exe scripts/check_env.py
说明：本脚本不连接数据库、不调用 LLM，只做导入与装配层面的验证。
"""
from __future__ import annotations

import sys
import importlib
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# (import 名, pip 包说明, 是否必需)
THIRD_PARTY = [
    ("fastapi", "fastapi", True),
    ("uvicorn", "uvicorn[standard]", True),
    ("pydantic", "pydantic", True),
    ("pydantic_settings", "pydantic-settings", True),
    ("psycopg", "psycopg[binary,pool]", True),
    ("psycopg_pool", "psycopg-pool（随 psycopg[pool]）", True),
    ("pgvector", "pgvector", True),
    ("multipart", "python-multipart", True),
    ("openai", "openai", True),
    ("langgraph", "langgraph", False),      # M3 使用，当前骨架延迟导入
    ("PyPDF2", "PyPDF2", True),            # PDF 文本抽取（loader._extract_pdf）
    ("docx", "python-docx", True),         # DOCX 段落抽取（loader._extract_docx）
    ("markdown", "markdown", False),
    ("numpy", "numpy", False),
]

# 项目内部模块（源码文件，不是 pip 安装对象；报错多为运行目录不对 / 文件缺失）
INTERNAL = [
    "config", "db", "errors", "app_logging", "prompts", "llm",
    "services.loader", "services.cleaner", "services.splitter",
    "services.embedder", "services.vector_store", "services.rag",
    "services.ingest", "services.memory", "services.reranker",
    "services.maintenance",
    "agent.state", "agent.graph", "agent.decision",
    "agent.tools.calc_tool", "agent.tools.search_tool", "agent.tools.doc_stats_tool",
    "services.chat_store",
    "routers.docs", "routers.session", "routers.chat", "routers.system",
]


def main() -> int:
    fail = 0

    print("== 1. 第三方 pip 依赖 ==")
    for mod_name, pkg, required in THIRD_PARTY:
        try:
            mod = importlib.import_module(mod_name)
            ver = getattr(mod, "__version__", "")
            tag = "必需" if required else "可选/后续"
            print(f"  [OK] {mod_name:18s} {ver:14s} ({tag}, pip: {pkg})")
        except Exception as exc:  # noqa: BLE001
            mark = "缺失(必需)" if required else "缺失(可选)"
            print(f"  [FAIL] {mod_name:18s} {mark} -> {type(exc).__name__}: {exc}；pip 安装：{pkg}")
            if required:
                fail += 1

    print("\n== 2. 项目内部模块（源码，非 pip 包）==")
    for mod_name in INTERNAL:
        try:
            importlib.import_module(mod_name)
            print(f"  [OK] {mod_name}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] {mod_name} -> {type(exc).__name__}: {exc}")
            fail += 1

    print("\n== 3. FastAPI 应用装配、路由注册与探活 ==")
    try:
        import main as app_module

        # 新版 FastAPI 的 include_router 为惰性 _IncludedRouter；OpenAPI schema 的 paths 是
        # 含完整前缀的权威端点清单（路由注册成功才会出现在 schema 中）。
        spec = app_module.app.openapi()
        all_paths = sorted(spec.get("paths", {}).keys())
        api_paths = [p for p in all_paths if p.startswith("/api")]
        print(f"  [OK] main.app 装配成功；OpenAPI 端点 {len(all_paths)} 个，/api 业务端点 {len(api_paths)} 个：")
        for p in api_paths:
            methods = ",".join(spec["paths"][p].keys()).upper()
            print(f"        - [{methods:6s}] {p}")

        # 不进入 with（不触发 lifespan，因此不连接数据库），仅验证路由可达、无 5xx
        try:
            from fastapi.testclient import TestClient

            client = TestClient(app_module.app)
            for url in ("/health", "/api/system/info"):
                resp = client.get(url)
                if resp.status_code < 500:
                    print(f"  [OK] GET {url} -> HTTP {resp.status_code}")
                else:
                    print(f"  [FAIL] GET {url} -> HTTP {resp.status_code}：{resp.text[:200]}")
                    fail += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  [WARN] TestClient 探活跳过（不影响导入结论）：{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] main.app 装配失败 -> {type(exc).__name__}: {exc}")
        fail += 1

    print("\n结果：" + ("全部通过 ✅" if fail == 0 else f"存在 {fail} 项失败 ❌"))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
