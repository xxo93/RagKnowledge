"""assess 之后的再规划（多轮主循环出口选择，架构 §4.2 / F-06 防死循环）。

两道收敛防线：
1. 规则门槛：`MULTI_ROUND_ENABLED` 关闭、达到 `MAX_TOOL_ROUNDS` 上限、无观察、最近工具失败，
   一律收敛到 answer，不做无效重试；
2. 防重复：LLM 给出的新 `(tool, args)` 若命中 `used_keys` 重复键，收敛到 answer；工具节点内另有
   同样的 dedup 保护做第二道防线。

默认 `LLM_REPLAN_ENABLED=False`：本模块不发起任何外部调用、恒收敛 answer，对话行为与单轮一致、
不增加 LLM / Embedding 费用；开启后由 `llm_replan_decision` 产出带新 `args` 的决策，供 replan
节点写回 `state.decision` 并回跳对应工具节点。

`plan_next(state) -> (route, new_decision)`：
- route：`knowledge_search|calculator|doc_stats|answer`，供条件边路由；
- new_decision：`{"tool","args","reason"}`；`None` 表示本轮收敛（不更新 decision）。
"""
from __future__ import annotations

import json
import logging

from config import settings
from agent import dedup

logger = logging.getLogger("rag.agent.replan")

_TOOLS = ("knowledge_search", "calculator", "doc_stats")


def _last_observation(state: dict) -> dict | None:
    obs = state.get("observations") or []
    return obs[-1] if obs else None


def _gate_allows(state: dict) -> bool:
    """确定性收敛门槛：任一不满足即不允许回跳工具。"""
    if not settings.MULTI_ROUND_ENABLED:
        return False
    if state.get("round", 0) >= settings.MAX_TOOL_ROUNDS:
        logger.info("达到工具轮次上限 %d，强制收敛到 answer", settings.MAX_TOOL_ROUNDS)
        return False
    last = _last_observation(state)
    if last is None:
        return False
    if not last.get("ok"):
        logger.info("最近工具未成功（%s），收敛到 answer 不做无效重试", last.get("tool"))
        return False
    return True


def _obs_brief(obs: dict) -> dict:
    """压缩观察结果，避免把完整 hits/来源塞进再规划提示词。"""
    result = obs.get("result") or {}
    if not isinstance(result, dict):
        return {"tool": obs.get("tool"), "ok": obs.get("ok")}
    return {
        "tool": obs.get("tool"),
        "ok": obs.get("ok"),
        "error": obs.get("error"),
        "sources": len(result.get("sources") or []),
        "result": result.get("result"),
    }


def llm_replan_decision(state: dict) -> dict | None:
    """LLM 再决策（仅 LLM_REPLAN_ENABLED=True 时由 plan_next 调用；默认关闭、不触达）。

    返回 {"tool", "args", "reason"}；不可用 / 解析失败 / 非合法工具一律返回 None（确定性收敛）。
    """
    if not settings.LLM_REPLAN_ENABLED:
        return None
    try:
        from llm import chat
        from prompts import REPLAN_SYSTEM_PROMPT

        brief = [_obs_brief(o) for o in (state.get("observations") or [])[-2:]]
        raw = chat(
            [
                {"role": "system", "content": REPLAN_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"query": state.get("query"), "observations": brief},
                        ensure_ascii=False,
                    ),
                },
            ],
            json_mode=True,
        )
        data = json.loads(raw or "{}")
        tool = data.get("next_tool") or data.get("tool")
        if tool in _TOOLS:
            return {
                "tool": tool,
                "args": data.get("args") or {},
                "reason": data.get("reason") or "LLM 再规划",
            }
        return None
    except Exception as exc:  # noqa: BLE001 - 再决策降级为收敛，不击穿主链路
        logger.warning("LLM 再决策失败，回落确定性收敛：%s", exc)
        return None


def plan_next(state: dict) -> tuple[str, dict | None]:
    """返回 (路由, 新决策)。默认（LLM 关闭）恒为 ("answer", None)。"""
    if not _gate_allows(state):
        return "answer", None

    if settings.LLM_REPLAN_ENABLED:
        decision = llm_replan_decision(state)
        if decision is not None:
            tool, args = decision["tool"], decision.get("args") or {}
            if dedup.is_duplicate(state.get("used_keys"), tool, args):
                logger.info("再规划命中重复键（%s），收敛避免循环", tool)
                return "answer", None
            return tool, decision
    return "answer", None


def decide_next(state: dict) -> str:
    """兼容包装：仅返回路由字符串（供条件边与离线单测使用）。"""
    return plan_next(state)[0]
