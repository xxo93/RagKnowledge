"""会话记忆底座（v3 / WP1）单元测试。

覆盖：
- answer_digest 生成与 token 估算；
- build_context 的注入块、预算裁剪、临时会话关闭、会话隔离；
- 滚动摘要触发条件与水位推进；
- 事实归一化、置信度过滤、规则抽取。
"""
from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from backend.services import memory, session_fact, session_summary


def test_make_digest_keeps_head_and_tail():
    answer = "开" * 400 + "尾" * 200
    digest = memory.make_digest(answer)
    assert digest.startswith("开" * 300)
    assert digest.endswith("尾" * 150)
    assert "……" in digest
    # 短答案原样保留
    assert memory.make_digest("简短回答") == "简短回答"


def test_estimate_tokens_monotonic():
    assert memory.estimate_tokens("") == 0
    assert memory.estimate_tokens("中文") > memory.estimate_tokens("ab")
    assert memory.estimate_tokens("a" * 400) < memory.estimate_tokens("中" * 400)


def _row(rid: int, query: str, answer: str, digest: Optional[str] = None) -> dict:
    return {"record_id": rid, "query": query, "answer": answer, "answer_digest": digest}


def test_build_context_orders_facts_summary_then_history(monkeypatch):
    state = {
        "session_id": 1,
        "session_name": "s",
        "summary": "用户在做月度报表。",
        "summarized_until_record_id": 3,
        "memory_enabled": True,
    }
    facts = [
        {"mem_id": 1, "kind": "constraint", "fact_key": "金额单位", "fact_value": "万元"},
        {"mem_id": 2, "kind": "entity", "fact_key": "项目名", "fact_value": "西湖项目"},
    ]
    rows = [_row(5, "最近问题", "最近回答", "最近摘要"), _row(4, "上一轮", "上一轮回答", None)]

    async def fake_state(session_id, user_id):
        return state if session_id == 1 and user_id == 7 else None

    async def fake_facts(session_id, user_id, limit=20):
        return facts if session_id == 1 and user_id == 7 else []

    async def fake_records(session_id, limit):
        return rows if session_id == 1 else []

    monkeypatch.setattr(memory.chat_store, "get_memory_state", fake_state)
    monkeypatch.setattr(memory.session_memory_store, "list_facts", fake_facts)
    monkeypatch.setattr(memory.chat_store, "context_records", fake_records)

    bundle = asyncio.run(memory.build_context(1, 7))
    assert bundle.memory_enabled is True
    blocks = bundle.extra_system()
    assert blocks[0].startswith("【本次会话已确认的信息】")
    assert "金额单位：万元" in blocks[0]
    assert blocks[1].startswith("【更早对话摘要】")
    assert bundle.history[0] == {"role": "user", "content": "上一轮"}
    assert bundle.history[-1] == {"role": "assistant", "content": "最近摘要"}
    # 没有 answer_digest 的历史记录会用 answer 现场生成
    assert bundle.history[1] == {"role": "assistant", "content": "上一轮回答"}
    assert bundle.usage["history_rounds"] == 2
    assert bundle.usage["facts_count"] == 2


def test_build_context_isolated_for_other_user(monkeypatch):
    async def fake_state(session_id, user_id):
        return None

    monkeypatch.setattr(memory.chat_store, "get_memory_state", fake_state)
    bundle = asyncio.run(memory.build_context(1, 99))
    assert bundle.history == []
    assert bundle.facts == []
    assert bundle.usage["memory_enabled"] is False


def test_build_context_disabled_skips_facts_and_summary(monkeypatch):
    state = {
        "session_id": 1,
        "session_name": "s",
        "summary": "不应注入的摘要",
        "summarized_until_record_id": 0,
        "memory_enabled": False,
    }
    rows = [_row(9, "当前问题", "当前回答", "当前摘要")]

    async def fake_state(session_id, user_id):
        return state

    async def fail_facts(*args, **kwargs):
        raise AssertionError("临时会话不得读取事实")

    async def fake_records(session_id, limit):
        return rows

    monkeypatch.setattr(memory.chat_store, "get_memory_state", fake_state)
    monkeypatch.setattr(memory.session_memory_store, "list_facts", fail_facts)
    monkeypatch.setattr(memory.chat_store, "context_records", fake_records)

    bundle = asyncio.run(memory.build_context(1, 7))
    assert bundle.extra_system() == []
    assert bundle.history  # 原文窗口仍保留（只关闭长期记忆）
    assert bundle.usage["summary_chars"] == 0


def test_build_context_budget_keeps_newest_rounds(monkeypatch):
    """预算不足时保留最新轮次，旧轮次被丢弃，事实/摘要仍保留。"""
    state = {
        "session_id": 1,
        "session_name": "s",
        "summary": "摘要" * 200,
        "summarized_until_record_id": 0,
        "memory_enabled": True,
    }
    facts = [{"mem_id": 1, "kind": "fact", "fact_key": "口径", "fact_value": "近120天"}]
    rows = [_row(10, "最新问题", "最新回答", "最新摘要")]
    for rid in range(9, 0, -1):
        rows.append(_row(rid, f"历史问题{rid}", "长回答" * 200, None))

    async def fake_state(session_id, user_id):
        return state

    async def fake_facts(session_id, user_id, limit=20):
        return facts

    async def fake_records(session_id, limit):
        return rows

    async def fake_cfg(key):
        return {"HISTORY_MAX_TOKENS": 700, "HISTORY_WINDOW_ROUNDS": 8, "HISTORY_TURN_MAX_TOKENS": 400}[key]

    monkeypatch.setattr(memory.chat_store, "get_memory_state", fake_state)
    monkeypatch.setattr(memory.session_memory_store, "list_facts", fake_facts)
    monkeypatch.setattr(memory.chat_store, "context_records", fake_records)
    monkeypatch.setattr(memory.app_config, "get_value", fake_cfg)

    bundle = asyncio.run(memory.build_context(1, 7))
    assert bundle.history[-2]["content"] == "最新问题"
    assert bundle.usage["history_rounds"] < 8
    assert bundle.usage["dropped_rounds"] > 0
    assert bundle.facts_block in bundle.extra_system()


def test_summary_not_triggered_below_threshold(monkeypatch):
    async def fake_cfg(key):
        return {"SUMMARY_ENABLED": True, "SUMMARY_TRIGGER_ROUNDS": 12, "SUMMARY_KEEP_ROUNDS": 8,
                "HISTORY_WINDOW_ROUNDS": 8}[key]

    async def fake_state(session_id, user_id):
        return {"summarized_until_record_id": 0, "memory_enabled": True, "summary": ""}

    async def fake_count(session_id, after):
        return 5

    monkeypatch.setattr(session_summary.app_config, "get_value", fake_cfg)
    monkeypatch.setattr(session_summary.chat_store, "get_memory_state", fake_state)
    monkeypatch.setattr(session_summary.chat_store, "count_records_after", fake_count)
    assert asyncio.run(session_summary.maybe_summarize(1, 7)) is None


def test_summary_advances_watermark(monkeypatch):
    rows = [_row(i, f"问题{i}", "回答", None) for i in range(1, 21)]

    async def fake_cfg(key):
        return {"SUMMARY_ENABLED": True, "SUMMARY_TRIGGER_ROUNDS": 12, "SUMMARY_KEEP_ROUNDS": 8,
                "HISTORY_WINDOW_ROUNDS": 8}[key]

    async def fake_state(session_id, user_id):
        return {"summarized_until_record_id": 0, "memory_enabled": True, "summary": "旧摘要"}

    async def fake_count(session_id, after):
        return len(rows)

    async def fake_records(session_id, after_id, until_id=None, limit=60):
        return rows

    async def fake_rule(previous, rows_):
        return "新摘要"

    async def fake_llm(previous, rows_, user_id):
        return "新摘要"

    captured = {}

    async def fake_update(session_id, user_id, summary, until_id):
        captured["until"] = until_id
        captured["summary"] = summary
        return True

    monkeypatch.setattr(session_summary.app_config, "get_value", fake_cfg)
    monkeypatch.setattr(session_summary.chat_store, "get_memory_state", fake_state)
    monkeypatch.setattr(session_summary.chat_store, "count_records_after", fake_count)
    monkeypatch.setattr(session_summary.chat_store, "records_between", fake_records)
    monkeypatch.setattr(session_summary.chat_store, "update_summary", fake_update)
    monkeypatch.setattr(session_summary, "_llm_summary", fake_llm)

    result = asyncio.run(session_summary.maybe_summarize(1, 7))
    assert result == "新摘要"
    # 保留最近 8 轮，水位推进到第 12 条
    assert captured["until"] == 12


def test_fact_normalize_and_validate():
    assert session_fact.normalize_key(" 金额 单位 ") == "金额单位"
    assert session_fact.normalize_key("币种") == "金额单位"
    items = [
        {"kind": "fact", "key": "金额单位", "value": "万元", "confidence": 0.9},
        {"kind": "fact", "key": "币种", "value": "元", "confidence": 0.3},  # 低置信
        {"kind": "unknown", "key": "x", "value": "y", "confidence": 0.9},   # 非法 kind
        {"kind": "constraint", "key": "", "value": "z", "confidence": 0.9}, # 空 key
    ]
    valid = session_fact.validate_items(items, min_confidence=0.6)
    assert valid == [{"kind": "fact", "key": "金额单位", "value": "万元", "confidence": 0.9}]


def test_rule_extract_constraint_and_todo():
    items = session_fact.rule_extract("请记住：以后金额都用万元。待办：周五前补交报表")
    kinds = {i["kind"] for i in items}
    assert "constraint" in kinds
    assert "todo" in kinds
    assert all(i["confidence"] >= 0.6 for i in items)
