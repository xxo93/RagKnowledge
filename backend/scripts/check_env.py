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
    sys.path.insert(0, str(BACKEND_DIR.parent))

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
# 包化后统一以 backend.<x> 绝对名导入（相对导入只在包内部语句里使用）
INTERNAL = [
    "backend.config", "backend.db", "backend.errors", "backend.app_logging",
    "backend.prompts", "backend.llm",
    "backend.services.loader", "backend.services.cleaner", "backend.services.splitter",
    "backend.services.embedder", "backend.services.vector_store", "backend.services.rag",
    "backend.services.ingest", "backend.services.memory", "backend.services.reranker",
    "backend.services.maintenance",
    "backend.services.security", "backend.services.account_store",
    "backend.services.auth_deps", "backend.services.audit",
    "backend.agent.state", "backend.agent.graph", "backend.agent.decision",
    "backend.agent.tools.calc_tool", "backend.agent.tools.search_tool",
    "backend.agent.tools.doc_stats_tool",
    "backend.services.chat_store",
    "backend.routers.docs", "backend.routers.session", "backend.routers.chat",
    "backend.routers.system", "backend.routers.auth", "backend.routers.admin",
]


def main() -> int:
    fail = 0

    # Windows 控制台默认 GBK，末尾的 ✅/❌ 会抛 UnicodeEncodeError 让脚本以异常收场；
    # 这里只放宽编码错误处理，不改变任何检测逻辑与输出语义。
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001 - 极老环境或已重定向时不支持 reconfigure
        pass

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
        import backend.main as app_module

        # 新版 FastAPI 的 include_router 为惰性 _IncludedRouter；OpenAPI schema 的 paths 是
        # 含完整前缀的权威端点清单（路由注册成功才会出现在 schema 中）。
        spec = app_module.app.openapi()
        all_paths = sorted(spec.get("paths", {}).keys())
        api_paths = [p for p in all_paths if p.startswith("/api")]
        print(f"  [OK] main.app 装配成功；OpenAPI 端点 {len(all_paths)} 个，/api 业务端点 {len(api_paths)} 个：")
        for p in api_paths:
            methods = ",".join(spec["paths"][p].keys()).upper()
            print(f"        - [{methods:6s}] {p}")

        # 不进入 with（不触发 lifespan，因此不连接数据库），仅验证路由可达、无 5xx。
        # /api/system/info 已收为管理员专属：未登录时 401 才是正确结果。
        try:
            from fastapi.testclient import TestClient

            client = TestClient(app_module.app)
            expectations = {"/health": (200,), "/api/system/info": (200, 401)}
            for url, expected in expectations.items():
                resp = client.get(url)
                if resp.status_code >= 500:
                    print(f"  [FAIL] GET {url} -> HTTP {resp.status_code}：{resp.text[:200]}")
                    fail += 1
                elif resp.status_code in expected:
                    note = "（未登录 401，鉴权生效）" if resp.status_code == 401 else ""
                    print(f"  [OK] GET {url} -> HTTP {resp.status_code}{note}")
                else:
                    print(f"  [FAIL] GET {url} -> HTTP {resp.status_code}（预期 {expected}）")
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
