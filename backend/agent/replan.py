"""Agent 再规划与收敛控制。

收敛规则：再规划开关关闭、达到工具轮次上限、缺少成功观察或最近工具失败时，
直接收敛到答案生成；LLM 再决策返回的工具参数若与历史调用重复，也收敛避免循环。
默认不启用 LLM 再决策，不产生额外模型调用；启用后由 LLM 决定是否回跳工具节点。
"""
from __future__ import annotations

import json
import logging

from ..config import settings
from . import dedup

logger = logging.getLogger("rag.agent.replan")

_TOOLS = ("knowledge_search", "calculator", "doc_stats", "biz_query")


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
        from ..llm import chat
        from ..prompts import REPLAN_SYSTEM_PROMPT

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
