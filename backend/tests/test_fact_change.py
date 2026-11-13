"""会话事实口径变更判定测试（M8 / WP5-8）。"""
from backend.services.session_memory_store import detect_change


def test_first_write_has_no_change_note():
    assert detect_change(None, "万元") == (None, False)


def test_same_value_is_not_a_change():
    assert detect_change("万元", "万元") == (None, False)


def test_different_value_keeps_previous():
    assert detect_change("元", "万元") == ("元", True)


def test_trailing_spaces_do_not_count_as_change():
    assert detect_change("万元", " 万元 ") == (None, False)
