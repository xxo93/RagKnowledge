"""工具调用防重复键纯函数单测（离线、不连数据库/网络）。

运行（backend/）：python -m pytest tests/test_agent_dedup.py -v
"""
from backend.agent.dedup import call_key, normalize_args, is_duplicate


def test_search_key_normalizes_extra_whitespace():
    a = call_key("knowledge_search", {"query": "VPN  怎么\n申请开通"})
    b = call_key("knowledge_search", {"query": "VPN 怎么申请开通"})
    assert a == b


def test_search_key_differs_on_distinct_query():
    a = call_key("knowledge_search", {"query": "年假怎么申请"})
    b = call_key("knowledge_search", {"query": "VPN 怎么申请"})
    assert a != b


def test_calc_key_is_stable_for_same_expression():
    a = call_key("calculator", {"expression": "12 + 3"})
    b = call_key("calculator", {"expression": "12 + 3"})
    assert a == b


def test_doc_stats_key_distinct_by_stat_type():
    assert call_key("doc_stats", {"stat_type": "total_docs"}) != call_key(
        "doc_stats", {"stat_type": "failed_docs"}
    )


def test_is_duplicate_detects_repeat_and_allows_new_tool():
    seen = [call_key("knowledge_search", {"query": "VPN 怎么申请"})]
    assert is_duplicate(seen, "knowledge_search", {"query": "VPN 怎么申请"})
    assert not is_duplicate(seen, "calculator", {"expression": "1+1"})
    assert not is_duplicate(None, "knowledge_search", {"query": "任意"})


def test_normalize_args_safe_on_missing_or_non_dict():
    assert normalize_args("knowledge_search", None) == {}
    assert normalize_args("doc_stats", {"stat_type": "total_docs"}) == {"stat_type": "total_docs"}
