"""文档级确定性摘要纯函数离线测试。"""
from services.vector_store import summarize_chunks


def test_summarize_chunks_truncates_to_max_len():
    rows = [
        {"content": "第一段内容，包含政策说明。"},
        {"content": "第二段内容，补充细则要求。"},
    ]
    summary = summarize_chunks(rows, max_len=12)
    assert len(summary) <= 12
    assert summary.startswith("第一段内容")


def test_summarize_chunks_skips_empty_content():
    rows = [{"content": "   "}, {"content": "有效切片文本"}]
    assert summarize_chunks(rows, max_len=120) == "有效切片文本"
