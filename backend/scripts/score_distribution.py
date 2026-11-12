"""阈值分布只读分析（S1）：纯向量余弦召回分数分布，用于决定 RETRIEVAL_MIN_SCORE。

- 直接调用 vs.search（绕过 Rerank 与应用层阈值），统计每条问题各候选的余弦相似度；
- 区分“期望主命中”与“其他候选”的分数区间，判断是否存在可分离的阈值空档；
- 只读：不入库、不删除、不调用 Rerank / LLM；输出建议阈值区间（不自动修改配置）。

运行（cwd=backend，Embedding Key 可用）：
    ..\\.venv\\Scripts\\python.exe scripts\\score_distribution.py
"""
from __future__ import annotations

import asyncio
import selectors
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import backend.db  # noqa: E402
from backend.config import settings  # noqa: E402
from backend.services import embedder as embed_mod, vector_store as vs  # noqa: E402

# 与 assess_retrieval.py 标注集一致：期望文档 + 问题
CASES = [
    ("leave_policy.md", [
        "员工入职满一年有多少天年假，申请要提前多久提交",
        "病假和事假分别怎么审批，未休完的年假能不能跨年累积",
    ]),
    ("attendance_rule.md", [
        "迟到补卡每个月允许几次，逾期还能不能补",
        "旷工几天按严重违纪处理，全勤奖如何计扣",
    ]),
    ("it_account.md", [
        "远程办公如何申请开通 VPN，登录需要什么认证",
        "忘记账号密码怎么重置，连续输错几次会被临时锁定",
    ]),
    ("security_policy.md", [
        "机密文档对外发送有什么限制，传输要怎么加密",
        "员工离职时敏感数据和系统权限应当如何处理",
    ]),
]


def _summ(name: str, vals: list[float]) -> str:
    if not vals:
        return f"{name}: 无样本"
    return (
        f"{name}: n={len(vals)} min={min(vals):.4f} "
        f"p50={statistics.median(vals):.4f} mean={statistics.mean(vals):.4f} max={max(vals):.4f}"
    )


async def main() -> int:
    if not await db.init_pool():
        print("DB_NOT_READY：数据库未就绪")
        return 2

    try:
        em = embed_mod.get_embedder()
        hit_scores: list[float] = []
        other_scores: list[float] = []
        print(f"=== 纯向量余弦分布（TOP_K={settings.TOP_K}，未经过 Rerank / 阈值过滤）===\n")

        for fname, queries in CASES:
            for q in queries:
                vec = em.embed_query(q)
                hits = await vs.search(vec, settings.TOP_K)
                ranked = [(h["doc_name"], round(float(h["score"]), 4)) for h in hits]
                exp_rank = next((i + 1 for i, (n, _) in enumerate(ranked) if n == fname), None)
                print(f"Q: {q}")
                print(f"   期望={fname} 命中排名={exp_rank}")
                for i, (n, s) in enumerate(ranked, 1):
                    mark = "  <== 期望" if n == fname else ""
                    print(f"   {i}. {n} score={s}{mark}")
                    if n == fname:
                        hit_scores.append(s)
                    else:
                        other_scores.append(s)
                print()

        print("--- 分布汇总 ---")
        print(_summ("期望主命中分数", hit_scores))
        print(_summ("其他候选分数", other_scores))

        gap_low = max(other_scores) if other_scores else 0.0
        gap_high = min(hit_scores) if hit_scores else 1.0
        print(f"\n主命中最低分={gap_high:.4f}，其他候选最高分={gap_low:.4f}")
        if gap_low < gap_high:
            mid = round((gap_low + gap_high) / 2, 4)
            print(f"存在可分离空档：建议阈值落在 ({gap_low:.4f}, {gap_high:.4f})，保守可取中值 {mid}")
            print("NOTE_SUGGESTION=SEPARABLE")
        else:
            print("主命中与其他候选分数重叠，小语料上不建议收紧阈值（易误截边缘相关片段）；建议保持 RETRIEVAL_MIN_SCORE=0.0，待大语料复评。")
            print("NOTE_SUGGESTION=KEEP_ZERO_ON_SMALL_CORPUS")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"DIST_EXCEPTION={type(exc).__name__}: {exc}")
        return 1
    finally:
        await db.close_pool()


def _selector_factory() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop(selectors.DefaultSelector())


if __name__ == "__main__":
    sys.exit(asyncio.run(main(), loop_factory=_selector_factory))
