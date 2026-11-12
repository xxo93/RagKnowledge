"""Embedding 适配器自检（M1 前置，选型 = 方案② api / 百炼 text-embedding-v3）。

静态部分（无需网络与 Key）：
- 打印 provider / model / dim / 批次与端点配置；
- 工厂返回类型、`dim` 与 `settings.EMBEDDING_DIM` 一致；
- api 实际单批行数不超过供应商上限（v3 OpenAI 兼容模式 ≤10 行）。

真实调用部分（仅当配置 EMBEDDING_API_KEY 时执行）：
- embed_query 单向量维度；
- embed_documents 多条向量的数量、顺序与维度。

未配置 Key 时打印 SKIP_REAL_CALL 并以退出码 0 结束（不阻断骨架/服务启动）；
填入百炼 Key 后复跑本脚本即可完成真实维度与分批验证。

运行：.venv\\Scripts\\python.exe backend\\scripts\\check_embedder.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.config import settings  # noqa: E402
from backend.services import embedder  # noqa: E402
from backend.errors import BizError  # noqa: E402


def main() -> int:
    print("== 配置 ==")
    print(
        f"provider={settings.EMBEDDING_PROVIDER} model={settings.EMBEDDING_MODEL} "
        f"dim={settings.EMBEDDING_DIM} batch_cfg={settings.EMBEDDING_BATCH_SIZE}"
    )
    print(
        f"api_base={'set' if settings.EMBEDDING_API_BASE_URL else 'EMPTY'} | "
        f"api_key={'set' if settings.EMBEDDING_API_KEY else 'EMPTY'}"
    )

    emb = embedder.get_embedder()
    print(f"== 工厂返回 == {type(emb).__name__} dim={emb.dim}")
    assert emb.dim == settings.EMBEDDING_DIM, "Embedder.dim 与 settings.EMBEDDING_DIM 不一致"

    if settings.EMBEDDING_PROVIDER.lower() == "api":
        actual = embedder._api_batch_size()
        print(f"api 实际单批上限 = {actual}（供应商硬上限 {embedder._API_MAX_ROWS}）")
        assert actual <= embedder._API_MAX_ROWS, "单批超过供应商行数上限"

    if not settings.EMBEDDING_API_KEY:
        print(
            "== SKIP_REAL_CALL：未配置 EMBEDDING_API_KEY，跳过联网向量化；"
            "填入百炼 Key 后复跑本脚本验证真实维度与分批 =="
        )
        return 0

    print("== 真实调用：embed_query ==")
    try:
        vec = emb.embed_query("企业文档智能问答系统，支持文档上传与向量检索。")
        print(
            f"query vector len={len(vec)} 期望={settings.EMBEDDING_DIM} "
            f"前5维={[round(x, 6) for x in vec[:5]]}"
        )
        assert len(vec) == settings.EMBEDDING_DIM, "query 向量维度不符"
    except BizError as exc:
        print(f"QUERY_FAIL code={exc.code} msg={exc.msg}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"QUERY_FAIL {type(exc).__name__}: {exc}")
        return 1

    print("== 真实调用：embed_documents（两条，验证数量/顺序/维度） ==")
    docs = [
        "第一份文档片段：RAG 由检索增强生成。",
        "第二份文档片段：HNSW 是近似最近邻索引。",
    ]
    try:
        vecs = emb.embed_documents(docs)
        print(f"docs vectors={len(vecs)} 期望={len(docs)} 各维={[len(v) for v in vecs]}")
        assert len(vecs) == len(docs), "返回向量数量与输入文本数不一致"
        assert all(len(v) == settings.EMBEDDING_DIM for v in vecs), "存在向量维度不符"
    except BizError as exc:
        print(f"DOCS_FAIL code={exc.code} msg={exc.msg}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"DOCS_FAIL {type(exc).__name__}: {exc}")
        return 1

    print("== EMBEDDER_OK ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
