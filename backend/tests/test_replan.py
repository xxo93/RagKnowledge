"""assess 再规划（多轮主循环出口）离线单测（零外部调用、纯函数）。

覆盖：框架开关回退、轮次上限强制收敛、无观察收敛、失败不重试、成功默认收敛、
plan_next 默认不产出新决策、LLM 扩展点产出新决策、重复决策被拦截、默认不调用 LLM。
"""
from __future__ import annotations

from agent import replan, dedup
from config import settings


def _state(round_: int = 0, observations: list | None = None, used_keys: list | None = None) -> dict:
    return {"round": round_, "observations": observations or [], "used_keys": used_keys or []}


def _obs(tool: str, ok: bool) -> dict:
    return {"tool": tool, "ok": ok, "result": {}, "error": None if ok else "boom"}


def test_disabled_framework_collapses_to_answer(monkeypatch):
    monkeypatch.setattr(settings, "MULTI_ROUND_ENABLED", False)
    state = _state(0, [_obs("knowledge_search", True)])
    assert replan.decide_next(state) == "answer"


def test_round_limit_forces_answer(monkeypatch):
    monkeypatch.setattr(settings, "MULTI_ROUND_ENABLED", True)
    state = _state(settings.MAX_TOOL_ROUNDS, [_obs("knowledge_search", True)])
    assert replan.decide_next(state) == "answer"


def test_no_observation_collapses(monkeypatch):
    monkeypatch.setattr(settings, "MULTI_ROUND_ENABLED", True)
    assert replan.decide_next(_state(0, [])) == "answer"


def test_failed_observation_does_not_retry(monkeypatch):
    monkeypatch.setattr(settings, "MULTI_ROUND_ENABLED", True)
    state = _state(0, [_obs("calculator", False)])
    assert replan.decide_next(state) == "answer"


def test_successful_search_default_collapses(monkeypatch):
    monkeypatch.setattr(settings, "MULTI_ROUND_ENABLED", True)
    monkeypatch.setattr(settings, "LLM_REPLAN_ENABLED", False)
    state = _state(0, [_obs("knowledge_search", True)])
    assert replan.decide_next(state) == "answer"
    assert replan.plan_next(state) == ("answer", None)


def test_successful_stats_default_collapses(monkeypatch):
    monkeypatch.setattr(settings, "MULTI_ROUND_ENABLED", True)
    monkeypatch.setattr(settings, "LLM_REPLAN_ENABLED", False)
    state = _state(0, [_obs("doc_stats", True)])
    assert replan.decide_next(state) == "answer"


def test_plan_next_default_no_decision(monkeypatch):
    monkeypatch.setattr(settings, "MULTI_ROUND_ENABLED", True)
    monkeypatch.setattr(settings, "LLM_REPLAN_ENABLED", False)
    state = _state(1, [_obs("knowledge_search", True)])
    route, new_decision = replan.plan_next(state)
    assert route == "answer" and new_decision is None


def test_llm_replan_extension_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "MULTI_ROUND_ENABLED", True)
    monkeypatch.setattr(settings, "LLM_REPLAN_ENABLED", True)
    fake = {"tool": "calculator", "args": {"expression": "12*8"}, "reason": "补充计算"}
    monkeypatch.setattr(replan, "llm_replan_decision", lambda s: fake)
    state = _state(1, [_obs("knowledge_search", True)])
    route, new_decision = replan.plan_next(state)
    assert route == "calculator"
    assert new_decision is not None and new_decision["tool"] == "calculator"
    assert replan.decide_next(state) == "calculator"


def test_plan_next_rejects_duplicate_decision(monkeypatch):
    monkeypatch.setattr(settings, "MULTI_ROUND_ENABLED", True)
    monkeypatch.setattr(settings, "LLM_REPLAN_ENABLED", True)
    args = {"expression": "1+1"}
    used = [dedup.call_key("calculator", args)]
    fake = {"tool": "calculator", "args": args, "reason": "重复调用"}
    monkeypatch.setattr(replan, "llm_replan_decision", lambda s: fake)
    state = _state(1, [_obs("calculator", True)], used_keys=used)
    route, new_decision = replan.plan_next(state)
    assert route == "answer" and new_decision is None


def test_llm_replan_not_called_by_default(monkeypatch):
    monkeypatch.setattr(settings, "MULTI_ROUND_ENABLED", True)
    monkeypatch.setattr(settings, "LLM_REPLAN_ENABLED", False)
    calls: list[int] = []
    monkeypatch.setattr(replan, "llm_replan_decision", lambda s: calls.append(1) or None)
    state = _state(1, [_obs("knowledge_search", True)])
    route, new_decision = replan.plan_next(state)
    assert route == "answer" and new_decision is None
    assert calls == []
