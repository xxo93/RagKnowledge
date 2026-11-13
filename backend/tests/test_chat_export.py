"""会话导出渲染单元测试（v3 / WP5-5）。"""
import json

from backend.routers.chat import _render_session_json, _render_session_markdown

SESSION = {"session_id": 7, "session_name": "测试会话", "memory_enabled": True}
RECORDS = [
    {
        "record_id": 11,
        "query": "年假怎么算？",
        "answer": "按司龄计算。",
        "trace_id": "abc123",
        "intent": "knowledge",
        "answer_mode": "knowledge",
        "source_ref": [
            {"doc_name": "leave_policy.md", "doc_title": "请假制度", "chunk_index": 2, "score": 0.91, "snippet": "年假"}
        ],
        "tool_call_info": [{"name": "knowledge_search", "label": "知识库检索", "status": "done", "round": 1}],
        "create_time": "2026-11-12 10:00:00",
    }
]
FACTS = [{"kind": "preference", "fact_key": "称呼", "fact_value": "小王"}]


def test_markdown_contains_trace_sources_tools_and_memory():
    md = _render_session_markdown(SESSION, RECORDS, FACTS, "用户问过年假")
    assert "# 测试会话" in md
    assert "trace_id：abc123" in md
    assert "《请假制度》片段 #2" in md
    assert "知识库检索（round 1，done）" in md
    assert "小王" in md
    assert "用户问过年假" in md


def test_json_export_shape():
    data = json.loads(_render_session_json(SESSION, RECORDS, FACTS, "摘要"))
    assert data["session"]["session_id"] == 7
    assert data["records"][0]["trace_id"] == "abc123"
    assert data["records"][0]["sources"][0]["doc_name"] == "请假制度"
    assert data["records"][0]["tools"][0]["name"] == "knowledge_search"
    assert data["facts"][0]["fact_key"] == "称呼"


def test_export_tolerates_bad_items():
    bad = dict(RECORDS[0], source_ref=["bad", None], tool_call_info={"not": "a list"})
    md = _render_session_markdown(SESSION, [bad], [], "")
    data = json.loads(_render_session_json(SESSION, [bad], [], ""))
    assert "来源" not in md
    assert data["records"][0]["sources"] == []
    assert data["records"][0]["tools"] == []
