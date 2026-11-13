"""v3 / WP3 运行日志单元测试：脱敏、trace、队列丢弃、LLM usage 采集。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.app_logging import normalize_trace_id
from backend.services import io_log


def test_mask_text_and_ip() -> None:
    src = "联系 13812345678 或 440101199001011234，邮箱 alice@example.com"
    out = io_log.mask_text(src)
    assert "13812345678" not in out and "138****5678" in out
    assert "440101199001011234" not in out and "440101********1234" in out
    assert "alice@example.com" not in out and "al***@example.com" in out
    assert io_log.mask_ip("203.0.113.9") == "203.0.*.*"
    assert io_log.mask_ip("2001:db8:1234::1") == "2001:db8:****"
    assert io_log.mask_ip(None) is None


def test_trace_context_roundtrip() -> None:
    token = io_log.set_trace_id("abcdef1234567890")
    assert io_log.get_trace_id() == "abcdef1234567890"
    io_log.reset_trace_id(token)
    # 重置后按需生成新值，长度固定 16
    assert len(io_log.get_trace_id()) == 16


def test_normalize_trace_id_rejects_unsafe() -> None:
    assert normalize_trace_id("abcdef1234567890") == "abcdef1234567890"
    for bad in ("short", "x" * 40, "bad value!", "中文trace123456789"):
        assert normalize_trace_id(bad) != bad


def test_queue_drop_counts_without_raising(monkeypatch) -> None:
    monkeypatch.setattr(io_log, "_loop", None)
    monkeypatch.setattr(io_log, "QUEUE", asyncio.Queue(maxsize=2))
    monkeypatch.setattr(io_log, "DROPPED", 0)
    io_log.enqueue_turn(trace_id="t1", query="q")
    io_log.enqueue_turn(trace_id="t2", query="q")
    io_log.enqueue_turn(trace_id="t3", query="q")  # 队满，丢弃
    assert io_log.QUEUE.qsize() == 2
    assert io_log.DROPPED == 1

    # 清理队列，避免影响其他用例
    while not io_log.QUEUE.empty():
        io_log.QUEUE.get_nowait()
        io_log.QUEUE.task_done()
    monkeypatch.setattr(io_log, "QUEUE", None)


def test_note_and_take_usage() -> None:
    io_log.note_llm_usage("trace-usage", 120, 30)
    io_log.note_llm_usage("trace-usage", 10, 5)
    assert io_log.take_usage("trace-usage") == (130, 35)
    assert io_log.take_usage("trace-usage") == (None, None)


def test_chat_logs_usage(monkeypatch) -> None:
    captured: list[dict] = []
    monkeypatch.setattr(io_log, "enqueue_llm", lambda **kw: captured.append(kw))
    usage = SimpleNamespace(prompt_tokens=11, completion_tokens=7)
    resp = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="hi"))], usage=usage)
    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: resp)))
    monkeypatch.setattr("backend.llm.get_client", lambda: fake)

    from backend import llm

    assert llm.chat([{"role": "user", "content": "x"}], purpose="plan") == "hi"
    assert captured[-1]["status"] == "ok"
    assert captured[-1]["purpose"] == "plan"
    assert captured[-1]["prompt_tokens"] == 11 and captured[-1]["completion_tokens"] == 7
    assert io_log.take_usage(captured[-1]["trace_id"]) == (11, 7)


def test_chat_stream_collects_usage(monkeypatch) -> None:
    captured: list[dict] = []
    monkeypatch.setattr(io_log, "enqueue_llm", lambda **kw: captured.append(kw))

    chunks = [
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="你"))], usage=None),
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="好"))], usage=None),
        SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=20, completion_tokens=2)),
    ]
    seen: dict = {}

    def create(**kwargs):
        seen.update(kwargs)
        return iter(chunks)

    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr("backend.llm.get_client", lambda: fake)

    from backend import llm

    async def run():
        return [t async for t in llm.chat_stream([{"role": "user", "content": "x"}], purpose="answer")]

    assert asyncio.run(run()) == ["你", "好"]
    assert seen["stream_options"] == {"include_usage": True}
    assert captured[-1]["status"] == "ok"
    assert captured[-1]["prompt_tokens"] == 20 and captured[-1]["completion_tokens"] == 2
