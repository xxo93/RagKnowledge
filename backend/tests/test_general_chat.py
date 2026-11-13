"""M2 / WP2 通用对话与意图路由单测（不访问真实模型/数据库）。"""
from __future__ import annotations

import asyncio

import pytest

from backend.agent import decision as d


def test_keyword_general_code_and_writing():
    code = d.keyword_decision("用 Python 写一个快速排序")
    assert code["intent"] == "general" and code["tool"] is None
    writing = d.keyword_decision("把这段话润色得更正式")
    assert writing["intent"] == "general"
    hello = d.keyword_decision("你好")
    assert hello["intent"] == "general"


def test_keyword_keeps_internal_questions_on_knowledge():
    assert d.keyword_decision("请假制度")["intent"] == "knowledge"
    assert d.keyword_decision("")["intent"] == "knowledge"
    # 纯算式 / 统计 / 业务库 仍优先于通用
    assert d.keyword_decision("1234*56")["intent"] == "calculator"
    assert d.keyword_decision("知识库一共有多少份文档")["intent"] == "doc_stats"


def test_internal_guard_terms_and_phrases():
    assert d.internal_guard("公司报销怎么走", ["差旅报销制度"])
    assert d.internal_guard("什么是审批流程", []) is not None
    assert d.internal_guard("写一个快速排序", []) is None


def test_general_intent_is_downgraded_by_internal_guard(monkeypatch):
    """LLM 判 general，但命中内部指向词 → 强制 knowledge 并记录原因。"""
    monkeypatch.setattr(
        d, "llm_plan_decision",
        lambda q: {"intent": "general", "tool": None, "args": {}, "confidence": 0.9, "reason": "llm"},
    )
    out = d.decide("解释一下什么是审批流程")
    assert out["intent"] == "knowledge"
    assert out["tool"] == "knowledge_search"
    assert out["degraded_reason"]


def test_general_intent_downgraded_by_term_dictionary(monkeypatch):
    monkeypatch.setattr(
        d, "llm_plan_decision",
        lambda q: {"intent": "general", "tool": None, "args": {}, "confidence": 0.9, "reason": "llm"},
    )
    out = d.decide("把差旅报销制度改写得更口语", internal_terms=["差旅报销制度"])
    assert out["intent"] == "knowledge"
    assert "差旅报销制度" in (out.get("degraded_reason") or "")


def test_explicit_general_skips_guard(monkeypatch):
    """显式「仅通用」不降级，但标记 forced_general，回答层会声明未使用内部资料。"""
    monkeypatch.setattr(d, "llm_plan_decision", lambda q: pytest.fail("显式模式不应再调 LLM"))
    out = d.decide("公司报销流程是什么", mode="general")
    assert out["intent"] == "general" and out["tool"] is None
    assert out["forced_general"] is True


def test_explicit_knowledge_suppresses_general(monkeypatch):
    monkeypatch.setattr(
        d, "llm_plan_decision",
        lambda q: {"intent": "general", "tool": None, "args": {}, "confidence": 0.9, "reason": "llm"},
    )
    out = d.decide("用 Python 写个快速排序", mode="knowledge")
    assert out["intent"] == "knowledge" and out["suppressed_intent"] == "general"


def test_explicit_biz_and_workflow_modes(monkeypatch):
    monkeypatch.setattr(d, "llm_plan_decision", lambda q: pytest.fail("显式模式不应再调 LLM"))
    assert d.decide("查一下订单", mode="biz")["tool"] == "biz_query"
    flow = d.decide("先查文档再导出", mode="workflow")
    assert flow["intent"] == "workflow" and flow["tool"] is None


def test_continuity_followup_keeps_previous_intent(monkeypatch):
    monkeypatch.setattr(d, "llm_plan_decision", lambda q: pytest.fail("上下文延续不应再调 LLM"))
    out = d.decide("再改短一点", mode="auto", last_intent="general")
    assert out["intent"] == "general" and "承接上一轮" in out["reason"]
    out2 = d.decide("那第三条呢", mode="auto", last_intent="knowledge")
    assert out2["intent"] == "knowledge"


def test_stream_general_uses_general_prompt(monkeypatch):
    from backend.prompts import GENERAL_SYSTEM_PROMPT
    from backend.services import rag

    captured = {}

    async def fake_stream(messages, temperature=None, **kwargs):
        captured["messages"] = messages
        captured["temperature"] = temperature
        captured["purpose"] = kwargs.get("purpose")
        yield "答案"

    monkeypatch.setattr("backend.llm.chat_stream", fake_stream)
    tokens = asyncio.run(_collect(rag.stream_general("写个快排", [{"role": "user", "content": "hi"}], temperature=0.7)))
    assert tokens == ["答案"]
    assert captured["messages"][0]["content"] == GENERAL_SYSTEM_PROMPT
    assert captured["temperature"] == 0.7
    # 通用链路不注入事实/摘要块
    assert not any("【会话事实】" in m["content"] for m in captured["messages"])
    assert captured["messages"][-1] == {"role": "user", "content": "写个快排"}


async def _collect(ait):
    return [x async for x in ait]
