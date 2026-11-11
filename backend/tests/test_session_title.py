"""会话首问标题提炼纯函数离线测试（零网络、零数据库）。"""
from services import session_title


def test_strips_punctuation_and_trailing_particles():
    assert session_title.derive_title("年假怎么申请？") == "年假怎么申请"


def test_collapses_newlines_and_whitespace():
    assert session_title.derive_title("VPN\n怎么 开通") == "VPN怎么开通"


def test_truncates_to_configured_length_with_ellipsis():
    title = session_title.derive_title("MySQL 的索引失效场景有哪些需要重点排查和优化的地方")
    assert len(title) <= 17  # 16 字 + 省略号
    assert title.endswith("…")


def test_empty_or_symbol_only_returns_blank():
    assert session_title.derive_title("") == ""
    assert session_title.derive_title("？？！！") == ""
    assert session_title.derive_title("   ") == ""


def test_keeps_alphanumeric_content():
    assert session_title.derive_title("1234*56 等于多少") == "123456等于多少"
