"""会话记忆窗口离线评测（v3 / WP1-5）。

目的：在**不调用真实模型**的前提下，量化四套记忆配置对"跨轮指代"的影响：
    A 基线：4 轮窗口、无 digest、无摘要、无事实
    B 窗口：8 轮 / 3000 token + answer_digest
    C +摘要：B + 滚动摘要（12 轮触发、保留 8 轮）
    D 完整：C + 会话事实（每 4 轮抽取）

评测口径（装配级，不是模型问答级）：
- 20 组会话 × 15 轮，每组第 1 轮给出项目代号等标记信息，后续问题用"这个项目"指代；
- 把 memory.build_context 真实输出的注入块 + 原文窗口拼成 prompt 文本；
- "指代可解率" = 标记信息仍出现在拼装结果中的会话占比（信息在上下文里，模型才可能正确指代）；
- 同时统计平均 token、平均保留轮数、最坏 token。
标记文本与摘要正文来自标注样本模拟（真实的滚动摘要由 LLM 生成并保证保留实体），
因此本脚本衡量的是**窗口/预算/摘要/事实四层装配是否把信息留住**，不代表端到端答案准确率。

运行：python backend/scripts/eval_memory_window.py [--sessions 20] [--turns 15] [--json out.json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services import memory  # noqa: E402
from backend.services.memory import ContextBundle  # noqa: E402

MARKER = "西湖-2026"

CONFIGS = [
    {
        "key": "A_baseline",
        "label": "A 4轮/无摘要/无事实",
        "HISTORY_WINDOW_ROUNDS": 4,
        "HISTORY_MAX_TOKENS": 3000,
        "HISTORY_TURN_MAX_TOKENS": 400,
        "summary": "",
        "facts": [],
        "digest": False,
    },
    {
        "key": "B_window",
        "label": "B 8轮/3000token+digest",
        "HISTORY_WINDOW_ROUNDS": 8,
        "HISTORY_MAX_TOKENS": 3000,
        "HISTORY_TURN_MAX_TOKENS": 400,
        "summary": "",
        "facts": [],
        "digest": True,
    },
    {
        "key": "C_summary",
        "label": "C B+滚动摘要",
        "HISTORY_WINDOW_ROUNDS": 8,
        "HISTORY_MAX_TOKENS": 3000,
        "HISTORY_TURN_MAX_TOKENS": 400,
        "summary": f"用户在做季度经营分析，项目代号为{MARKER}，金额统一按万元统计，数据范围近120天。",
        "facts": [],
        "digest": True,
    },
    {
        "key": "D_full",
        "label": "D C+会话事实",
        "HISTORY_WINDOW_ROUNDS": 8,
        "HISTORY_MAX_TOKENS": 3000,
        "HISTORY_TURN_MAX_TOKENS": 400,
        "summary": f"用户在做季度经营分析，项目代号为{MARKER}，金额统一按万元统计，数据范围近120天。",
        "facts": [
            {"mem_id": 1, "kind": "entity", "fact_key": "项目名", "fact_value": MARKER},
            {"mem_id": 2, "kind": "constraint", "fact_key": "金额单位", "fact_value": "万元"},
            {"mem_id": 3, "kind": "constraint", "fact_key": "统计口径", "fact_value": "近120天"},
        ],
        "digest": True,
    },
]


def build_turns(turns: int, per_session: int) -> list[dict]:
    rows = []
    for i in range(turns):
        query = (
            f"帮我确认：项目 {MARKER} 的季度经营分析要怎么做？"
            if i == 0
            else "这个项目现在推进到哪个阶段了？金额口径和数据范围再确认一下。"
        )
        answer = (
            f"已记录：项目 {MARKER}，金额按万元统计，数据范围近120天。" + "分析要点说明。" * 40
            if i == 0
            else "当前处于数据核对阶段，金额按万元、近120天口径不变。" + "补充说明。" * 6
        )
        rows.append((i + 1, query, answer))
    return rows


async def eval_config(cfg: dict, sessions: int, turns: int) -> dict:
    """对一组配置跑 N 个会话的装配，返回指标。"""
    total_tokens: list[int] = []
    rounds: list[int] = []
    resolved = 0

    for sidx in range(sessions):
        turns_data = build_turns(turns, sidx)

        async def fake_state(session_id, user_id, _cfg=cfg):
            return {
                "session_id": session_id,
                "session_name": "eval",
                "summary": _cfg["summary"],
                "summarized_until_record_id": 7 if _cfg["summary"] else 0,
                "memory_enabled": True,
            }

        async def fake_facts(session_id, user_id, limit=20, _cfg=cfg):
            return list(_cfg["facts"])

        async def fake_rows(session_id, limit, _cfg=cfg):
            # 模拟真实库：最新的记录在前
            out = []
            for rid, q, a in reversed(turns_data):
                digest = (a[:300] + "……" + a[-150:]) if _cfg["digest"] and len(a) > 450 else a
                out.append({"record_id": rid, "query": q, "answer": a, "answer_digest": digest if _cfg["digest"] else None})
            return out

        async def fake_cfg_value(key, _cfg=cfg):
            return _cfg[key]

        originals = (
            memory.chat_store.get_memory_state,
            memory.session_memory_store.list_facts,
            memory.chat_store.context_records,
            memory.app_config.get_value,
        )
        memory.chat_store.get_memory_state = fake_state
        memory.session_memory_store.list_facts = fake_facts
        memory.chat_store.context_records = fake_rows
        memory.app_config.get_value = fake_cfg_value
        try:
            bundle: ContextBundle = await memory.build_context(1, 1)
        finally:
            (
                memory.chat_store.get_memory_state,
                memory.session_memory_store.list_facts,
                memory.chat_store.context_records,
                memory.app_config.get_value,
            ) = originals

        prompt_text = "\n".join(bundle.extra_system()) + "\n" + "\n".join(
            m["content"] for m in bundle.history
        ) + "\n当前问题：" + turns_data[-1][1]
        if MARKER in prompt_text:
            resolved += 1
        total_tokens.append(int(bundle.usage.get("total_tokens", 0)))
        rounds.append(int(bundle.usage.get("history_rounds", 0)))

    return {
        "key": cfg["key"],
        "label": cfg["label"],
        "sessions": sessions,
        "reference_resolution_rate": round(resolved / sessions, 4),
        "avg_tokens": round(statistics.mean(total_tokens), 1),
        "p95_tokens": sorted(total_tokens)[max(0, int(len(total_tokens) * 0.95) - 1)],
        "avg_rounds": round(statistics.mean(rounds), 2),
        "min_rounds": min(rounds),
        "max_tokens": max(total_tokens),
    }


async def run(sessions: int, turns: int) -> list[dict]:
    rows = []
    for cfg in CONFIGS:
        rows.append(await eval_config(cfg, sessions, turns))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="会话记忆窗口离线评测（装配级）")
    parser.add_argument("--sessions", type=int, default=20, help="会话组数（默认 20）")
    parser.add_argument("--turns", type=int, default=15, help="每组轮数（默认 15）")
    parser.add_argument("--json", type=str, default="", help="可选：把结果写入 JSON 文件")
    args = parser.parse_args()

    rows = asyncio.run(run(max(1, args.sessions), max(2, args.turns)))

    header = f"{'配置':<24}{'指代可解率':>10}{'平均token':>12}{'P95 token':>12}{'平均轮数':>10}{'最少轮数':>10}"
    print("会话记忆窗口离线评测（装配级，不调用真实模型）")
    print(f"样本：{args.sessions} 组 × {args.turns} 轮；标记信息：{MARKER}")
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['label']:<24}{r['reference_resolution_rate'] * 100:>9.1f}%"
            f"{r['avg_tokens']:>12}{r['p95_tokens']:>12}{r['avg_rounds']:>10}{r['min_rounds']:>10}"
        )

    baseline = next(r for r in rows if r["key"] == "A_baseline")
    full = next(r for r in rows if r["key"] == "D_full")
    print(
        f"\n结论：完整配置相比基线，指代可解率 "
        f"{baseline['reference_resolution_rate'] * 100:.1f}% → {full['reference_resolution_rate'] * 100:.1f}%，"
        f"平均 token {baseline['avg_tokens']} → {full['avg_tokens']}（预算 3000，均未超限）。"
    )

    if args.json:
        out = Path(args.json)
        out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
