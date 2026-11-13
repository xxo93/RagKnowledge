"""N9 query 改写单元测试：触发条件、防漂移校验、关闭开关、缓存复用。"""
import asyncio
import json

from backend.services import query_rewrite as qr


def test_should_rewrite_short_followup_with_history():
    history = [
        {"role": "user", "content": "差旅报销的住宿费标准是多少"},
        {"role": "assistant", "content": "一线城市 500 元/晚。"},
    ]
    assert qr.should_rewrite("那住宿费呢", "knowledge", history) is True
    assert qr.should_rewrite("还有呢", "knowledge", history) is True


def test_should_not_rewrite_first_turn_or_standalone_question():
    assert qr.should_rewrite("那住宿费呢", "knowledge", []) is False
    history = [{"role": "user", "content": "差旅报销标准"}, {"role": "assistant", "content": "…"}]
    assert qr.should_rewrite("2025年差旅报销的住宿费标准是多少？", "knowledge", history) is False


def test_valid_keeps_numbers_and_entities():
    ok, _ = qr._valid("那住宿费呢", "2025年差旅报销的住宿费标准是多少")
    assert ok is True
    ok, why = qr._valid("住宿费 500 元", "差旅报销住宿费标准")
    assert ok is False and "500" in why


def test_rewrite_disabled_returns_original(monkeypatch):
    async def fake_get_value(key):
        return False

    monkeypatch.setattr(qr.app_config, "get_value", fake_get_value)
    history = [{"role": "user", "content": "上轮问题"}, {"role": "assistant", "content": "上轮回答"}]
    result = asyncio.run(qr.rewrite(
        session_id=1, query="那住宿费呢", history=history, last_intent="knowledge",
        last_record_id=9, mode="auto", intent="knowledge",
    ))
    assert result["used"] is False
    assert result["rewritten_query"] == "那住宿费呢"
    assert result["enabled"] is False


def test_rewrite_uses_json_and_cache(monkeypatch):
    calls = []

    def fake_chat(messages, **kwargs):
        calls.append(kwargs)
        return json.dumps({
            "need_rewrite": True,
            "rewritten_query": "2025年差旅报销的住宿费标准是多少",
            "reason": "承接住宿费追问",
        }, ensure_ascii=False)

    async def fake_get_value(key):
        return True if key == "QUERY_REWRITE_ENABLED" else 5000

    monkeypatch.setattr(qr.app_config, "get_value", fake_get_value)
    monkeypatch.setattr(qr.llm, "_model_for", lambda purpose: "small-x")
    monkeypatch.setattr(qr.llm, "chat", fake_chat)
    qr.clear_cache()
    history = [{"role": "user", "content": "差旅报销住宿费标准"}, {"role": "assistant", "content": "500 元/晚"}]
    first = asyncio.run(qr.rewrite(
        session_id=1, query="那住宿费呢", history=history, last_intent="knowledge",
        last_record_id=9, mode="auto", intent="knowledge",
    ))
    second = asyncio.run(qr.rewrite(
        session_id=1, query="那住宿费呢", history=history, last_intent="knowledge",
        last_record_id=9, mode="auto", intent="knowledge",
    ))
    assert first["used"] is True
    assert first["rewritten_query"].startswith("2025年")
    assert len(calls) == 1
    assert second["cached"] is True
    assert second["rewritten_query"] == first["rewritten_query"]
    assert calls[0]["purpose"] == "query_rewrite"


def test_rewrite_model_failure_falls_back(monkeypatch):
    async def fake_get_value(key):
        return True if key == "QUERY_REWRITE_ENABLED" else 3000

    def boom(*args, **kwargs):
        raise RuntimeError("timeout")

    monkeypatch.setattr(qr.app_config, "get_value", fake_get_value)
    monkeypatch.setattr(qr.llm, "chat", boom)
    qr.clear_cache()
    history = [{"role": "user", "content": "差旅报销标准"}, {"role": "assistant", "content": "…"}]
    result = asyncio.run(qr.rewrite(
        session_id=2, query="那住宿费呢", history=history, last_intent="knowledge",
        last_record_id=1, mode="auto", intent="knowledge",
    ))
    assert result["used"] is False
    assert result["rewritten_query"] == "那住宿费呢"
    assert "回落原句" in result["reason"]
