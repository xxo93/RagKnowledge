"""来源富字段组装纯函数离线测试（不连接向量库）。"""
from config import settings
from services.rag import _doc_title, _summary, _source_fields


def test_doc_title_removes_extension_and_underscore():
    assert _doc_title("mysql_index.pdf") == "mysql index"
    assert _doc_title("leave_policy.md") == "leave policy"


def test_summary_uses_first_sentence():
    content = "年假可在入职后申请。转正后额度按司龄累计。"
    assert _summary(content) == "年假可在入职后申请"


def test_source_fields_preserve_ids_and_rerank_score():
    hit = {
        "chunk_id": 7,
        "doc_id": 9,
        "doc_name": "security_policy.md",
        "chunk_index": 3,
        "score": 0.823456,
        "content": "访问权限最小化。\n第二行内容",
        "doc_summary": "安全策略概述",
        "rerank_score": 0.91,
    }
    fields = _source_fields(hit)
    assert fields["doc_title"] == "security policy"
    assert fields["chunk_id"] == 7
    assert fields["doc_id"] == 9
    assert fields["score"] == 0.8235
    assert fields["rerank_score"] == 0.91
    assert fields["doc_summary"] == "安全策略概述"
    assert fields["summary"] == "访问权限最小化"
    assert fields["excerpt"].startswith("访问权限最小化")
    assert len(fields["excerpt"]) <= settings.SOURCE_EXCERPT_CHARS


def test_source_fields_without_rerank_score_omits_key():
    fields = _source_fields({
        "chunk_id": 1, "doc_id": 2, "doc_name": "a.txt", "chunk_index": 0,
        "score": 0.5, "content": "纯文本",
    })
    assert "rerank_score" not in fields
