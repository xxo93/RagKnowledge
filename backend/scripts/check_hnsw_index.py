"""HNSW 向量索引验证（#9 遗留项）：合成占位向量造数 -> EXPLAIN ANALYZE -> 事务回滚清理。

目标（如实验证，不伪造结论）：
1. 确认 HNSW 余弦索引 `idx_chunk_embedding`（vector_cosine_ops）真实存在于 document_chunk；
2. 在足够规模下执行与生产检索同构的 `ORDER BY embedding <=> q LIMIT 5`，打印执行计划，
   如实判断优化器是否选中该索引（Index/Bitmap Index Scan）；若仍为 Seq Scan，说明当前规模
   成本未触发索引，随数据量增长后再评估，脚本不修改任何留存样料。

数据安全：
- 造数全部放在单个未提交事务内，EXPLAIN ANALYZE 只读不写；结束一律 rollback；
- 兜底按专用 doc_name='__hnsw_probe__' 删除，绝不触碰 4 篇留存样料；
- 结束打印最终 document/chunk 计数，必须回到造数前水平。

运行（cwd=backend，同步 psycopg，不调外网/LLM/Rerank）：
    python scripts/check_hnsw_index.py
退出码：0=脚本正常完成并清理干净；1=异常或清理后计数未复原。
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402
from config import settings  # noqa: E402

PROBE_NAME = "__hnsw_probe__"
PROBE_PATH = "__probe__"
# 合成向量规模：仅用于让优化器看到非平凡行数；事务内造数、结束回滚，不落盘留存
PROBE_ROWS = 1500
PLAN_LIMIT = 5


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def _random_unit(rnd: random.Random, dim: int) -> list[float]:
    v = [rnd.gauss(0.0, 1.0) for _ in range(dim)]
    norm = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / norm for x in v]


def main() -> int:
    conn = psycopg.connect(settings.DB_DSN, connect_timeout=5)
    try:
        before_doc, before_chunk = conn.execute(
            "SELECT (SELECT count(*) FROM document), (SELECT count(*) FROM document_chunk)"
        ).fetchone()
        print(f"BEFORE_COUNTS document={before_doc} chunk={before_chunk} dim={settings.EMBEDDING_DIM}")

        # 索引存在性（元数据查询，与造数无关）
        idx = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename='document_chunk' AND indexname='idx_chunk_embedding'"
        ).fetchone()
        print("HNSW_INDEX_EXISTS =", "YES" if idx else "NO")
        if idx:
            print("INDEX_DEF =", idx[0].strip())

        rnd = random.Random(42)
        dim = settings.EMBEDDING_DIM
        probe_id = None
        try:
            # 专用文档元记录（未提交事务内）
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO document (doc_name, file_path, file_size, status, chunk_count) "
                "VALUES (%s, %s, 0, 1, %s) RETURNING doc_id",
                (PROBE_NAME, PROBE_PATH, PROBE_ROWS),
            )
            probe_id = int(cur.fetchone()[0])

            inserted = 0
            for i in range(PROBE_ROWS):
                vec = _vec_literal(_random_unit(rnd, dim))
                cur.execute(
                    "INSERT INTO document_chunk (doc_id, chunk_index, content, embedding) "
                    "VALUES (%s, %s, %s, %s::vector)",
                    (probe_id, i, f"probe chunk {i}", vec),
                )
                inserted += 1
            print(f"INSERTED_PROBE_ROWS={inserted}（未提交事务，结束回滚）")

            qvec = _vec_literal(_random_unit(rnd, dim))
            plan = cur.execute(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) "
                "SELECT c.chunk_id, c.doc_id, d.doc_name, c.chunk_index, c.content, "
                "       1 - (c.embedding <=> %s::vector) AS score "
                "FROM document_chunk c JOIN document d ON d.doc_id = c.doc_id "
                "ORDER BY c.embedding <=> %s::vector LIMIT %s",
                (qvec, qvec, PLAN_LIMIT),
            ).fetchall()
            plan_text = "\n".join(r[0] for r in plan)
            print("\n--- EXPLAIN ANALYZE（生产同构 Top-K 余弦检索）---")
            for line in plan_text.splitlines()[:18]:
                print(line)

            used_index = "idx_chunk_embedding" in plan_text and (
                "Index Scan" in plan_text or "Bitmap Index Scan" in plan_text
            )
            print("\nPLAN_USED_INDEX =", "YES" if used_index else "NO")
            if not used_index:
                print("NOTE: 当前规模优化器选择顺序扫描；HNSW 索引已存在，随真实语料增长再评估触发条件。")
        finally:
            # 关键安全动作：回滚未提交造数，杜绝合成向量进入留存数据
            conn.rollback()
            conn.execute(
                "DELETE FROM document_chunk WHERE doc_id IN "
                "(SELECT doc_id FROM document WHERE doc_name=%s)",
                (PROBE_NAME,),
            )
            conn.execute("DELETE FROM document WHERE doc_name=%s", (PROBE_NAME,))
            conn.commit()

        after_doc, after_chunk = conn.execute(
            "SELECT (SELECT count(*) FROM document), (SELECT count(*) FROM document_chunk)"
        ).fetchone()
        print(f"AFTER_COUNTS  document={after_doc} chunk={after_chunk}")
        restored = (after_doc, after_chunk) == (before_doc, before_chunk)
        print("DATA_RESTORED =", "YES" if restored else "NO")
        if not restored:
            print("ERROR: 计数未复原，需人工核查 probe 残留！")
            return 1
        return 0
    except Exception as exc:  # noqa: BLE001
        print("HNSW_PROBE_EXCEPTION =", type(exc).__name__, str(exc))
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
