"""文本清洗与切片单测（services/cleaner.py、services/splitter.py），无外部依赖。"""
from services.cleaner import clean_text
from services.splitter import split_text, chunk_with_index


def test_clean_strips_control_chars_and_extra_blank_lines():
    raw = "第一段内容。\x00\x0b\n\n\n\n第二段内容。\n第 3 页\n"
    out = clean_text(raw)
    assert "\x00" not in out and "\x0b" not in out
    assert "第 3 页" not in out          # 疑似页码行被剔除
    assert "\n\n\n" not in out            # 多余空行被合并
    assert "第一段内容。" in out


def test_splitter_overlap_and_nonempty():
    text = "A" * 1200
    chunks = split_text(text, chunk_size=500, overlap=100)
    assert len(chunks) >= 2
    assert all(c.strip() for c in chunks)
    # 相邻块存在重叠
    assert chunks[0][-100:] == chunks[1][:100]


def test_splitter_empty_input():
    assert split_text("", 500, 100) == []
    assert split_text("   \n  ", 500, 100) == []


def test_chunk_index_assignment():
    rows = chunk_with_index(["a", "b"])
    assert [r["chunk_index"] for r in rows] == [0, 1]
