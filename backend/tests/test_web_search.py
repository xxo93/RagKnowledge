"""联网搜索服务单测（不发起真实请求，httpx.AsyncClient 被替换为假客户端）。

覆盖：
1. 阿里百炼：请求体带 enable_search/forced_search/enable_source，解析 search_info 来源 + 答案；
2. 博查 / Tavily 结构仍可解析（配置中心切供应商后不退化）；
3. 未开启 / 未配置 Key / 非 200 / 空结果 / 超时 的错误码，且都不抛出；
4. 来源切换：provider 取值影响地址、鉴权方式与 Key 归属；
5. is_ready() 只在"开关打开 + 该供应商有 Key"时为 True。
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from backend.config import settings
from backend.services import web_search


class _FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    @property
    def text(self):
        return str(self._payload)


class _FakeClient:
    """替换 httpx.AsyncClient：把 post 的返回值或异常由外部注入。"""

    response: _FakeResponse | None = None
    exc: Exception | None = None
    last: dict = {}

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeClient.last = {"url": url, "json": json, "headers": headers}
        if _FakeClient.exc:
            raise _FakeClient.exc
        assert _FakeClient.response is not None
        return _FakeClient.response


@pytest.fixture()
def fake_http(monkeypatch):
    _FakeClient.response = None
    _FakeClient.exc = None
    monkeypatch.setattr(web_search.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


@pytest.fixture()
def bailian(monkeypatch):
    """百炼供应商 + 独立 Key，清掉可能存在的其他提供商 Key。"""
    monkeypatch.setattr(settings, "WEB_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "WEB_SEARCH_PROVIDER", "bailian")
    monkeypatch.setattr(settings, "BAILIAN_SEARCH_API_KEY", "sk-ws-bailian")
    monkeypatch.setattr(settings, "WEB_SEARCH_MODEL", "qwen-plus")
    monkeypatch.setattr(settings, "WEB_SEARCH_BASE_URL", "https://dashscope.aliyuncs.com/api/v1")
    monkeypatch.setattr(settings, "WEB_SEARCH_MAX_RESULTS", 5)
    monkeypatch.setattr(settings, "WEB_SEARCH_TIMEOUT", 20.0)
    monkeypatch.setattr(settings, "WEB_SEARCH_STRATEGY", "turbo")


def _bailian_payload() -> dict:
    return {
        "output": {
            "choices": [{"message": {"role": "assistant", "content": "LPR 持平于 3.6%。"}}],
            "search_info": {
                "search_results": [
                    {"index": 1, "title": "央行公告", "url": "https://example.com/a", "site_name": "央行"},
                    {"index": 2, "title": "", "url": "", "snippet": "无链接条目应被丢弃"},
                ]
            },
        }
    }


def test_bailian_success(fake_http, bailian):
    fake_http.response = _FakeResponse(200, _bailian_payload())
    result = asyncio.run(web_search.ask("LPR 多少"))

    assert result.ok is True
    assert len(result.items) == 1
    assert result.items[0]["url"] == "https://example.com/a"
    assert result.items[0]["source_type"] == "web"
    assert result.items[0]["title"] == "央行公告"
    # 百炼一次调用就带 grounded 答案，上层不需要再生成一次
    assert result.answer == "LPR 持平于 3.6%。"

    # 请求形状：DashScope 原生 text-generation + enable_search + 强制搜索 + 返回来源
    assert fake_http.last["url"].endswith("/services/aigc/text-generation/generation")
    assert fake_http.last["headers"]["Authorization"] == "Bearer sk-ws-bailian"
    params = fake_http.last["json"]["parameters"]
    assert params["enable_search"] is True
    assert params["search_options"]["forced_search"] is True
    assert params["search_options"]["enable_source"] is True
    assert params["search_options"]["search_strategy"] == "turbo"
    assert fake_http.last["json"]["model"] == "qwen-plus"


def test_bailian_key_falls_back_to_embedding(fake_http, bailian, monkeypatch):
    """独立变量为空时回落到同厂商的 EMBEDDING_API_KEY（不是跨厂商借 Key）。"""
    monkeypatch.setattr(settings, "BAILIAN_SEARCH_API_KEY", "")
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "sk-ws-embedding")
    fake_http.response = _FakeResponse(200, _bailian_payload())
    asyncio.run(web_search.ask("LPR"))
    assert fake_http.last["headers"]["Authorization"] == "Bearer sk-ws-embedding"


def test_bailian_answer_falls_back_to_output_text(fake_http, bailian):
    """兼容默认 result_format：没有 choices 时取 output.text。"""
    fake_http.response = _FakeResponse(
        200,
        {
            "output": {
                "text": "答案在 text 字段",
                "search_info": {"search_results": [{"title": "T", "url": "https://e.com/1"}]},
            }
        },
    )
    result = asyncio.run(web_search.ask("x"))
    assert result.ok is True
    assert result.answer == "答案在 text 字段"


def test_bailian_no_sources_is_empty(fake_http, bailian):
    """搜到了答案但没来源 → 视为联网不可用，不让模型自答冒充。"""
    fake_http.response = _FakeResponse(200, {"output": {"choices": [{"message": {"content": "自答"}}]}})
    result = asyncio.run(web_search.ask("x"))
    assert result.ok is False
    assert result.error_code == web_search.ERR_EMPTY
    assert result.answer == ""


def test_bocha_success(fake_http, monkeypatch):
    monkeypatch.setattr(settings, "WEB_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "WEB_SEARCH_PROVIDER", "bocha")
    monkeypatch.setattr(settings, "WEB_SEARCH_API_KEY", "k")
    monkeypatch.setattr(settings, "WEB_SEARCH_BASE_URL", "https://api.bochaai.com/v1")
    monkeypatch.setattr(settings, "WEB_SEARCH_MAX_RESULTS", 5)
    fake_http.response = _FakeResponse(
        200,
        {
            "data": {
                "webPages": {
                    "value": [
                        {"name": "央行公告", "url": "https://example.com/a", "summary": "LPR 下调", "siteName": "央行"},
                        {"name": "", "url": "", "summary": "无链接条目应被丢弃"},
                    ]
                }
            }
        },
    )
    result = asyncio.run(web_search.ask("LPR"))

    assert result.ok is True
    assert len(result.items) == 1
    assert result.answer == ""  # 博查只给片段，答案交给上层生成
    assert fake_http.last["url"].endswith("/web-search")
    assert fake_http.last["headers"]["Authorization"] == "Bearer k"


def test_tavily_success(fake_http, monkeypatch):
    monkeypatch.setattr(settings, "WEB_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setattr(settings, "WEB_SEARCH_API_KEY", "tvly-x")
    monkeypatch.setattr(settings, "WEB_SEARCH_BASE_URL", "https://api.tavily.com")
    fake_http.response = _FakeResponse(
        200, {"results": [{"title": "RFC", "url": "https://example.com/rfc", "content": "摘要"}]}
    )
    result = asyncio.run(web_search.ask("rfc"))
    assert result.ok is True
    assert result.items[0]["title"] == "RFC"
    assert fake_http.last["url"].endswith("/search")


def test_disabled_returns_error_code(monkeypatch):
    monkeypatch.setattr(settings, "WEB_SEARCH_ENABLED", False)
    monkeypatch.setattr(settings, "BAILIAN_SEARCH_API_KEY", "k")
    result = asyncio.run(web_search.ask("今天的汇率"))
    assert result.ok is False
    assert result.error_code == web_search.ERR_DISABLED


def test_no_key_returns_error_code(monkeypatch):
    monkeypatch.setattr(settings, "WEB_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "WEB_SEARCH_PROVIDER", "bailian")
    monkeypatch.setattr(settings, "BAILIAN_SEARCH_API_KEY", "")
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "")
    result = asyncio.run(web_search.ask("今天的汇率"))
    assert result.ok is False
    assert result.error_code == web_search.ERR_NO_KEY
    assert web_search.describe_failure(result.error_code) == "未配置联网检索服务"


def test_http_error(fake_http, bailian):
    fake_http.response = _FakeResponse(401, {"error": "invalid key"})
    result = asyncio.run(web_search.ask("x"))
    assert result.ok is False
    assert result.error_code == web_search.ERR_HTTP


def test_timeout(fake_http, bailian):
    fake_http.exc = httpx.TimeoutException("timeout")
    result = asyncio.run(web_search.ask("x"))
    assert result.ok is False
    assert result.error_code == web_search.ERR_TIMEOUT
    assert web_search.describe_failure(result.error_code) == "联网检索超时"


def test_is_ready_requires_key_for_current_provider(monkeypatch):
    monkeypatch.setattr(settings, "WEB_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "WEB_SEARCH_PROVIDER", "bailian")
    monkeypatch.setattr(settings, "WEB_SEARCH_MODEL", "qwen-plus")
    monkeypatch.setattr(settings, "BAILIAN_SEARCH_API_KEY", "")
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "")
    monkeypatch.setattr(settings, "WEB_SEARCH_API_KEY", "sk-bocha")  # 别家 Key 不能冒充百炼
    assert web_search.is_ready() is False

    monkeypatch.setattr(settings, "BAILIAN_SEARCH_API_KEY", "sk-ws-bailian")
    assert web_search.is_ready() is True

    monkeypatch.setattr(settings, "WEB_SEARCH_ENABLED", False)
    assert web_search.is_ready() is False
    # 运行时配置显式传入时覆盖 .env 开关与供应商
    assert web_search.is_ready(True, "bailian") is True
    assert web_search.is_ready(True, "bing") is False


def test_unknown_provider_disabled(monkeypatch, fake_http):
    monkeypatch.setattr(settings, "WEB_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "WEB_SEARCH_PROVIDER", "bing")
    result = asyncio.run(web_search.ask("x"))
    assert result.ok is False
    assert result.error_code == web_search.ERR_DISABLED
