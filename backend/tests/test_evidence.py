"""依据强度判定纯函数测试（M8 / WP5-10）。"""
from backend.services import evidence


def test_knowledge_two_sources_is_strong():
    got = evidence.assess("knowledge", 2)
    assert got["level"] == "strong"
    assert got["label"] == "强"
    assert got["source_count"] == 2


def test_knowledge_one_source_is_medium():
    got = evidence.assess("knowledge", 1)
    assert got["level"] == "medium"
    assert got["label"] == "中"


def test_knowledge_zero_source_is_weak():
    assert evidence.assess("knowledge", 0)["level"] == "weak"


def test_web_and_general_are_weak_even_with_sources():
    assert evidence.assess("web", 3)["level"] == "weak"
    assert evidence.assess("general", 3)["level"] == "weak"


def test_biz_query_with_table_is_strong_without_table_is_medium():
    assert evidence.assess("biz_query", 0, has_table=True)["level"] == "strong"
    assert evidence.assess("biz_query", 0, has_table=False)["level"] == "medium"


def test_tool_is_medium_and_unknown_mode_uses_source_count():
    assert evidence.assess("tool", 0)["level"] == "medium"
    assert evidence.assess("", 2)["level"] == "strong"
    assert evidence.assess(None, 0)["level"] == "weak"
