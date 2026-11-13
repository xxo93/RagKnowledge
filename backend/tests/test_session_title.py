"""会话首问标题提炼纯函数离线测试（零网络、零数据库）。"""
import asyncio

from backend.services import session_title


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


def test_sanitize_llm_title_removes_prefix_quotes_and_first_line():
    assert session_title.sanitize_title("标题：“年假申请流程”") == "年假申请流程"
    assert session_title.sanitize_title("会话标题: 报销系统入口\n解释文字") == "报销系统入口"
    assert session_title.sanitize_title("「VPN 开通」") == "VPN开通"


def test_sanitize_llm_title_rejects_symbol_only_and_limits_length():
    assert session_title.sanitize_title("？？！！") == ""
    assert len(session_title.sanitize_title("一二三四五六七八九十一二三四五六七八", max_len=16)) == 16


def test_generate_title_falls_back_to_rule_when_switch_off(monkeypatch):
    async def disabled(_key):
        return False

    monkeypatch.setattr(session_title.app_config, "get_value", disabled)
    monkeypatch.setattr(session_title.llm, "chat", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调用模型")))
    assert asyncio.run(session_title.generate_title("年假怎么申请？", "", 1)) == "年假怎么申请"


def test_generate_title_falls_back_to_rule_when_llm_errors(monkeypatch):
    async def enabled(_key):
        return True

    async def prompt(_name, _user_id):
        return {"content": "system"}

    monkeypatch.setattr(session_title.app_config, "get_value", enabled)
    monkeypatch.setattr(session_title.prompt_store, "resolve", prompt)
    monkeypatch.setattr(session_title.llm, "chat", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert asyncio.run(session_title.generate_title("年假怎么申请？", "回答摘要", 1)) == "年假怎么申请"


