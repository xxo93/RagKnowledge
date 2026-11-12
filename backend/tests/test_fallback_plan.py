"""兜底决策单测（不联网、不连库）。

`_fallback_plan` 是"内部知识不足 → 走哪种兜底"的唯一决策点，规则来自需求文档 §3.2：
1. web：联网成功 → 联网答案 + 非内部能力提示条；联网失败 → 联系管理员 + 失败原因提示；
2. guide：返回引导话术，不联网；
3. contact_admin：返回联系管理员话术，不联网；
4. 联网开关关闭时 web 模式等同于"联网不可用"，仍回落 contact_admin。
"""
from __future__ import annotations

import asyncio

import pytest

from backend.routers import chat as chat_router
from backend.services import app_config, rag as rag_svc, web_search
from backend.config import settings


@pytest.fixture()
def config_stub(monkeypatch):
    """把配置读取改成内存字典，避免测试碰数据库。"""
    values = {
        "FALLBACK_MODE": "web",
        "FALLBACK_MIN_HITS": 2,
        "FALLBACK_TEXT_GUIDE": "请补充想查的文档或业务字段。",
        "FALLBACK_TEXT_CONTACT": "请联系管理员补充文档。",
        "WEB_SEARCH_ENABLED": True,
    }

    async def fake_get_value(key, default=None):
        return values.get(key, default)

    monkeypatch.setattr(app_config, "get_value", fake_get_value)
    return values


def test_guide_mode_returns_text_only(config_stub):
    config_stub["FALLBACK_MODE"] = "guide"
    plan = asyncio.run(chat_router._fallback_plan("不存在的知识", []))
    assert plan["answer_mode"] == chat_router.MODE_GUIDE
    assert plan["text"] == "请补充想查的文档或业务字段。"
    assert plan["sources"] == []
    assert plan["notice"] is None
    assert plan["web_items"] == []


def test_contact_admin_mode(config_stub):
    config_stub["FALLBACK_MODE"] = "contact_admin"
    plan = asyncio.run(chat_router._fallback_plan("不存在的知识", []))
    assert plan["answer_mode"] == chat_router.MODE_CONTACT
    assert plan["text"] == "请联系管理员补充文档。"
    assert plan["notice"] is None


def test_web_mode_success(config_stub, monkeypatch):
    items = [
        {"title": "央行公告", "url": "https://example.com/a", "snippet": "LPR 下调", "site": "央行"}
    ]

    async def fake_ask(query):
        return web_search.WebResult(True, items)

    monkeypatch.setattr(web_search, "ask", fake_ask)
    plan = asyncio.run(chat_router._fallback_plan("LPR 是多少", []))
    assert plan["answer_mode"] == chat_router.MODE_WEB
    assert plan["notice"]["code"] == "WEB_SEARCH_USED"
    assert "不属于企业内部知识库能力" in plan["notice"]["msg"]
    assert len(plan["sources"]) == 1
    assert plan["sources"][0]["source_type"] == "web"
    assert plan["sources"][0]["url"] == "https://example.com/a"
    assert plan["web_items"] == items


def test_web_mode_failure_falls_back_to_contact(config_stub, monkeypatch):
    async def fake_ask(query):
        return web_search.WebResult(False, [], web_search.ERR_TIMEOUT, "联网检索超时")

    monkeypatch.setattr(web_search, "ask", fake_ask)
    plan = asyncio.run(chat_router._fallback_plan("LPR 是多少", []))
    assert plan["answer_mode"] == chat_router.MODE_CONTACT
    assert plan["text"] == "请联系管理员补充文档。"
    assert plan["notice"]["code"] == "WEB_SEARCH_UNAVAILABLE"
    assert "联网检索超时" in plan["notice"]["msg"]
    assert plan["sources"] == []


def test_web_disabled_in_config_falls_back_to_contact(config_stub):
    config_stub["WEB_SEARCH_ENABLED"] = False
    plan = asyncio.run(chat_router._fallback_plan("LPR 是多少", []))
    assert plan["answer_mode"] == chat_router.MODE_CONTACT
    assert plan["notice"]["code"] == "WEB_SEARCH_UNAVAILABLE"
    assert "管理员关闭了联网检索" in plan["notice"]["msg"]


def test_web_sources_normalization_skips_items_without_url():
    items = [
        {"title": "有链接", "url": "https://example.com/a", "snippet": "s1", "site": "站点"},
        {"title": "无链接", "url": "", "snippet": "s2"},
    ]
    sources = rag_svc.web_sources(items)
    assert len(sources) == 1
    assert sources[0]["source_type"] == "web"
    assert sources[0]["doc_title"] == "有链接"
    assert sources[0]["rank"] == 1


def test_status_web_search_ready_follows_key(monkeypatch):
    """能力灯必须跟着"当前供应商的搜索 Key"走，不能用 LLM Key 冒充可用。"""
    monkeypatch.setattr(settings, "WEB_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "WEB_SEARCH_PROVIDER", "bailian")
    monkeypatch.setattr(settings, "WEB_SEARCH_MODEL", "qwen-plus")
    monkeypatch.setattr(settings, "BAILIAN_SEARCH_API_KEY", "")
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "")
    monkeypatch.setattr(settings, "WEB_SEARCH_API_KEY", "")
    monkeypatch.setattr(settings, "LLM_API_KEY", "sk-deepseek")
    assert web_search.is_ready() is False
    monkeypatch.setattr(settings, "BAILIAN_SEARCH_API_KEY", "sk-ws-bailian")
    assert web_search.is_ready() is True
