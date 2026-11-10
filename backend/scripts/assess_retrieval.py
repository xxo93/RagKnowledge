"""召回排名评测（随当前 RERANK_ENABLED 开关：false=纯向量余弦召回；true=向量召回后经 gte-rerank-v2 重排）。

目的：在留存样例语料上量化现有向量召回质量，输出可复现指标与逐条来源对照：
- Hit@1：Top1 命中期望文档的比例；
- Hit@K：期望文档进入前 K（K=TOP_K）的比例；
- MRR：期望文档排名倒数的均值；
- Top1 平均向量余弦分（score 字段恒为向量余弦分；Rerank 仅改变返回顺序、不覆盖 score）；
- 排名口径为最终返回顺序（开启 Rerank 时为重排后顺序）；逐条 query→Top1 文档/分数/期望排名。

数据策略（按用户决策）：
- 样料位于 backend/samples/*.md，入库后【保留不删除】，供人工对话测试；
- 同名文档已存在则复用 doc_id、跳过 ingest，避免重复产生 Embedding 调用费用；
- 本脚本不调用 delete_document，不清空任何数据。

运行（cwd=backend，Embedding Key 可用；首次会对 4 篇样料真实入库）：
    ..\\.venv\\Scripts\\python.exe scripts\\assess_retrieval.py
退出码：0=指标达到基线通过线；1=低于基线通过线（仍输出真实分数，不代表报错）；2=数据库未就绪。
"""
from __future__ import annotations

import asyncio
import selectors
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from config import settings  # noqa: E402
from services import ingest, rag, vector_store as vs  # noqa: E402

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"

# 标注集：期望文档文件名 + 该文档的评测问题（含一个同义改写）
CASES = [
    {
        "file": "leave_policy.md",
        "queries": [
            "员工入职满一年有多少天年假，申请要提前多久提交",
            "病假和事假分别怎么审批，未休完的年假能不能跨年累积",
        ],
    },
    {
        "file": "attendance_rule.md",
        "queries": [
            "迟到补卡每个月允许几次，逾期还能不能补",
            "旷工几天按严重违纪处理，全勤奖如何计扣",
        ],
    },
    {
        "file": "it_account.md",
        "queries": [
            "远程办公如何申请开通 VPN，登录需要什么认证",
            "忘记账号密码怎么重置，连续输错几次会被临时锁定",
        ],
    },
    {
        "file": "security_policy.md",
        "queries": [
            "机密文档对外发送有什么限制，传输要怎么加密",
            "员工离职时敏感数据和系统权限应当如何处理",
        ],
    },
]

# 基线通过线（观察用，不掩盖真实分数）
HIT1_LINE = 0.75
MRR_LINE = 0.70


async def ensure_ingest() -> dict[str, int]:
    """同名样料已存在则复用，否则真实入库；返回 {文件名: doc_id}。"""
    _, items = await vs.list_documents(1, 100)
    existing = {it["doc_name"]: it["doc_id"] for it in items}
    name2id: dict[str, int] = {}
    for case in CASES:
        fname = case["file"]
        if fname in existing:
            name2id[fname] = existing[fname]
            print(f"[REUSE] {fname} 已存在 doc_id={existing[fname]}，跳过入库（不重复计费）")
            continue
        data = (SAMPLES_DIR / fname).read_bytes()
        out = await ingest.process(fname, data)
        name2id[fname] = out["doc_id"]
        print(f"[INGEST] {fname} -> doc_id={out['doc_id']} status={out['status']} chunks={out['chunk_count']}")
    return name2id


async def main() -> int:
    if not await db.init_pool():
        print("SKIP_DB：数据库未就绪，请先完成 M0")
        return 2

    try:
        name2id = await ensure_ingest()
        k = int(settings.TOP_K)
        print(f"\n=== 召回评测（RERANK_ENABLED={settings.RERANK_ENABLED}，K={k}，样料 {len(CASES)} 篇）===")

        rows: list[dict] = []
        hit1 = hitk = 0
        rr = 0.0
        top1_scores: list[float] = []

        for case in CASES:
            exp_id = name2id[case["file"]]
            for q in case["queries"]:
                hits = await rag.retrieve(q)
                rank = next((i + 1 for i, h in enumerate(hits) if h["doc_id"] == exp_id), None)
                top1_name = hits[0]["doc_name"] if hits else "-"
                top1_score = round(float(hits[0]["score"]), 4) if hits else None
                is_hit1 = rank == 1
                in_topk = rank is not None and rank <= k
                hit1 += int(is_hit1)
                hitk += int(in_topk)
                rr += (1.0 / rank) if rank else 0.0
                if hits:
                    top1_scores.append(float(hits[0]["score"]))
                rows.append({
                    "q": q, "expect": case["file"], "top1": top1_name,
                    "score": top1_score, "rank": rank, "hit1": is_hit1, "hitk": in_topk,
                })

        n = len(rows)
        hit1_rate = hit1 / n
        hitk_rate = hitk / n
        mrr = rr / n
        mean_top1 = round(statistics.mean(top1_scores), 4) if top1_scores else 0.0

        print("\n--- 逐条对照 ---")
        for r in rows:
            flag = "HIT1" if r["hit1"] else ("TOPK" if r["hitk"] else "MISS")
            rank_txt = r["rank"] if r["rank"] is not None else "-"
            print(f"[{flag}] rank={rank_txt} top1={r['top1']} score={r['score']}")
            print(f"      Q: {r['q']}")
            print(f"      期望: {r['expect']}")

        print("\n--- 汇总指标 ---")
        print(f"样本数 n={n}")
        print(f"Hit@1 = {hit1}/{n} = {hit1_rate:.3f}（通过线 {HIT1_LINE}）")
        print(f"Hit@{k} = {hitk}/{n} = {hitk_rate:.3f}")
        print(f"MRR   = {mrr:.3f}（通过线 {MRR_LINE}）")
        print(f"Top1 平均相似度 = {mean_top1}")

        ok = hit1_rate >= HIT1_LINE and mrr >= MRR_LINE
        print("EVAL_RESULT =", "BASELINE_OK" if ok else "BELOW_LINE（如实记录，非运行报错）")
        return 0 if ok else 1
    except Exception as exc:  # noqa: BLE001
        print(f"EVAL_EXCEPTION = {type(exc).__name__}: {exc}")
        return 1
    finally:
        await db.close_pool()


def _selector_factory() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop(selectors.DefaultSelector())


if __name__ == "__main__":
    sys.exit(asyncio.run(main(), loop_factory=_selector_factory))
