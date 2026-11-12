"""A1（V51）动态多轮再规划门控轻量实测：零 DB / 零 Embedding / 零 Rerank。

目标：把 V49 的动态 LLM 再规划从“离线 mock 单测”补一层最小真实证据，同时严格控费。

覆盖项：
1. 默认 `LLM_REPLAN_ENABLED=False`：`plan_next` 恒收敛 `("answer", None)`，
   `llm_replan_decision` 直接返回 None、不触达模型（离线段，零费用）；
2. 规则门槛（离线段）：达到 `MAX_TOOL_ROUNDS` 上限 / 最近观察失败 / 无观察，一律 answer；
3. 重复键拦截与路由选择（离线段，注入决策、不联网）：命中 `used_keys` 重复键收敛 answer；
   非重复决策按工具名路由（knowledge_search / calculator 各一例）；
4. 联网段（仅当环境变量 `REPLAN_LIVE=1`，且全程只发起 **1 次** 真实 `llm.chat(json_mode)` 短调用）：
   真实 DeepSeek 再决策；返回合法工具或 null（主动收敛）都视为合法——本脚本验证的是“门控可调用、
   结构可解析、异常/非法可降级”，不诱导模型强行多调一次工具；不调用 Embedding / Rerank、不写数据库。

运行（cwd=backend）：
  ..\\.venv\\Scripts\\python.exe scripts\\check_replan_live.py                 # 纯离线零费用
  $env:REPLAN_LIVE="1"; ..\\.venv\\Scripts\\python.exe scripts\\check_replan_live.py   # 单次真实再决策
退出码：0=ALL_PASS；1=HAS_FAIL。进程结束前将 `LLM_REPLAN_ENABLED` 复位为 False，不写 .env、不改持久配置。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.config import settings  # noqa: E402
from backend.agent import replan, dedup  # noqa: E402

_LIVE = os.environ.get("REPLAN_LIVE") == "1"
_TOOLS = ("knowledge_search", "calculator", "doc_stats")


def _state(*, round_no: int = 1, ok: bool = True, has_obs: bool = True, used_keys=None):
    obs = (
        [
            {
                "tool": "knowledge_search",
                "ok": ok,
                "error": None if ok else "检索失败",
                "result": {"sources": [{"doc_name": "leave_policy.md"}], "result": None},
            }
        ]
        if has_obs
        else []
    )
    return {
        "query": "年假审批要点是什么，知识库一共有多少篇文档？",
        "round": round_no,
        "observations": obs,
        "used_keys": used_keys or [],
    }


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, label: str) -> None:
        print(("[PASS] " if cond else "[FAIL] ") + label)
        if not cond:
            failures.append(label)

    saved_flag = settings.LLM_REPLAN_ENABLED
    try:
        # ---------- 1. 默认关闭：恒收敛、不触网 ----------
        settings.LLM_REPLAN_ENABLED = False
        s_ok = _state(used_keys=[dedup.call_key("knowledge_search", {"query": "年假审批要点"})])
        route, decision = replan.plan_next(s_ok)
        check(route == "answer" and decision is None, "默认 LLM_REPLAN_ENABLED=False：plan_next 收敛 answer、无新决策")
        check(replan.llm_replan_decision(s_ok) is None, "默认关闭：llm_replan_decision 直接返回 None（不触达模型）")

        # ---------- 2. 规则门槛（离线） ----------
        gate, _ = replan.plan_next(_state(round_no=settings.MAX_TOOL_ROUNDS))
        check(gate == "answer", "达到 MAX_TOOL_ROUNDS 上限强制收敛 answer")
        gate, _ = replan.plan_next(_state(ok=False))
        check(gate == "answer", "最近工具失败时收敛 answer、不做无效重试")
        gate, _ = replan.plan_next(_state(has_obs=False))
        check(gate == "answer", "无观察时收敛 answer")

        # ---------- 3. 重复键拦截 + 路由（离线，注入决策不联网） ----------
        settings.LLM_REPLAN_ENABLED = True
        orig_fn = replan.llm_replan_decision
        try:
            # 3a. 注入与已用键完全相同的 knowledge_search → 重复 → answer
            fake_ks = {"tool": "knowledge_search", "args": {"query": "年假审批要点"}, "reason": "注入测试-重复"}
            replan.llm_replan_decision = lambda s: fake_ks  # type: ignore[assignment]
            r, d = replan.plan_next(s_ok)
            check(r == "answer" and d is None, "重复 (knowledge_search,相同 query) 被 dedup 拦截、收敛 answer")

            # 3b. 无重复键 + knowledge_search → 路由 knowledge_search 并带回新决策
            s_fresh = _state(used_keys=[])
            r, d = replan.plan_next(s_fresh)
            check(r == "knowledge_search" and d is not None and d["tool"] == "knowledge_search",
                  "非重复 knowledge_search 决策路由 knowledge_search 且写回新决策")

            # 3c. 注入 calculator 新决策 → 路由 calculator
            fake_calc = {"tool": "calculator", "args": {"expression": "12*8"}, "reason": "注入测试-补算"}
            replan.llm_replan_decision = lambda s: fake_calc  # type: ignore[assignment]
            r, d = replan.plan_next(_state(used_keys=[]))
            check(r == "calculator" and d is not None and d["args"]["expression"] == "12*8",
                  "非重复 calculator 决策路由 calculator 并保留 args")
        finally:
            replan.llm_replan_decision = orig_fn
            settings.LLM_REPLAN_ENABLED = False

        # ---------- 4. 可选联网段：单次真实再决策 ----------
        if _LIVE:
            print("--- REPLAN_LIVE=1：发起 1 次真实 LLM 再决策短调用（无 Embedding/Rerank、不写库）---")
            settings.LLM_REPLAN_ENABLED = True
            decision = replan.llm_replan_decision(_state(used_keys=[]))
            if decision is None:
                print("[INFO] 模型选择主动收敛（next_tool=null），属合法输出")
                check(True, "联网再决策正常返回且可降级为 None（主动收敛合法）")
            else:
                ok_struct = (
                    isinstance(decision, dict)
                    and decision.get("tool") in _TOOLS
                    and isinstance(decision.get("args"), dict)
                )
                print("[LIVE] 模型再决策 =", {
                    "tool": decision.get("tool"),
                    "args": decision.get("args"),
                    "reason": str(decision.get("reason"))[:80],
                })
                check(ok_struct, "联网再决策返回合法工具名与 args 结构")
                # 真实决策若为合法工具且非重复，plan_next 应路由到对应工具（不再重复调 LLM：直接喂已有决策验证路由）
                orig2 = replan.llm_replan_decision
                replan.llm_replan_decision = lambda s, _d=decision: _d  # type: ignore[assignment]
                try:
                    r, d = replan.plan_next(_state(used_keys=[]))
                    check(r == decision["tool"] and d is not None, "合法非重复真实决策经 plan_next 路由到对应工具")
                finally:
                    replan.llm_replan_decision = orig2
        else:
            print("--- REPLAN_LIVE 未置 1：跳过真实 LLM 调用（离线零费用）---")
    finally:
        settings.LLM_REPLAN_ENABLED = saved_flag  # 复位，绝不持久化开启

    print("REPLAN_LIVE_MODE =", "LIVE(1 real call)" if _LIVE else "OFFLINE(0 real calls)")
    print("REPLAN_LIVE_RESULT =", "ALL_PASS" if not failures else "HAS_FAIL")
    if failures:
        print("失败项：", "; ".join(failures))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
