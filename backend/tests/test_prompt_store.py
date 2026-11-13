"""WP5-7 提示词版本化：稳定分桶与内容校验的纯函数测试。"""
from __future__ import annotations

import pytest

from backend.services import prompt_store


def test_bucket_is_stable_and_bounded():
    """同一用户在同一提示词上始终落同一桶，且结果在 0–99。"""
    first = prompt_store.bucket("rag_answer", 42)
    assert first == prompt_store.bucket("rag_answer", 42)
    assert 0 <= first < 100


def test_bucket_differs_by_prompt_and_user():
    """不同提示词/用户应尽量分散，避免所有用户都命中同一侧。"""
    values = {
        prompt_store.bucket("rag_answer", 7),
        prompt_store.bucket("rag_answer", 8),
        prompt_store.bucket("general_answer", 7),
    }
    assert len(values) >= 2


def test_unknown_prompt_name_rejected():
    with pytest.raises(Exception) as exc:
        prompt_store._check_name("not_registered")
    assert "未知提示词" in str(exc.value)


def test_prompt_registry_contains_integrated_names():
    """登记表只包含真正接入链路的提示词，避免出现“能改但不生效”。"""
    assert {"rag_answer", "general_answer", "session_summary", "session_fact", "session_title", "query_rewrite"} <= set(
        prompt_store.PROMPT_REGISTRY
    )
