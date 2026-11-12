"""联网搜索服务（M3 兜底能力，M3.1 起默认走阿里百炼）。

职责边界：
1. **只做"发一次检索请求并解析结果"**，不决定是否兜底、不拼答案；兜底策略在 routers/chat.py。
2. **任何失败都不抛出**：无 Key / 未开启 / 超时 / 非 200 / 结果为空 一律返回 `ok=False` +
   `error_code`，由上层统一回落"联系管理员"话术，绝不用模型自答冒充联网结果。
3. **来源可热切换**：供应商 / 模型 / 地址 / 超时 / 结果数都从配置中心读（`app_config`），
   Key 只在 `.env` 维护，不下发到前端。

支持的供应商（同一 ask(query) 接口）：
- bailian（默认，阿里百炼）：DashScope 原生 text-generation + `enable_search`，
  一次调用同时拿到"带搜索依据的答案"与 `search_info.search_results` 结构化来源；
  Key 用独立变量 `BAILIAN_SEARCH_API_KEY`（值可与 EMBEDDING_API_KEY 相同，便于单独轮换）。
- bocha（博查）：POST {base}/web-search，Bearer 鉴权，取 data.webPages.value[]。
- tavily：POST {base}/search，body 里带 api_key，取 results[]。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx

from ..config import settings
from . import app_config

logger = logging.getLogger("rag.web_search")

# 失败原因代号：进 SSE notice 与前端提示，便于用户与运维区分"没配置"与"调不通"
ERR_DISABLED = "WEB_SEARCH_DISABLED"
ERR_NO_KEY = "WEB_SEARCH_NO_KEY"
ERR_TIMEOUT = "WEB_SEARCH_TIMEOUT"
ERR_HTTP = "WEB_SEARCH_HTTP_ERROR"
ERR_EMPTY = "WEB_SEARCH_EMPTY"

PROVIDER_LABELS = {"bailian": "阿里百炼", "bocha": "博查", "tavily": "Tavily"}
PROVIDER_OPTIONS = ("bailian", "bocha", "tavily")

# 各供应商默认地址与默认模型（配置中心留空时用这里的值）
DEFAULT_BASE = {
    "bailian": "https://dashscope.aliyuncs.com/api/v1",
    "bocha": "https://api.bochaai.com/v1",
    "tavily": "https://api.tavily.com",
}
DEFAULT_MODEL = {"bailian": "qwen-plus", "bocha": "", "tavily": ""}


@dataclass
class WebResult:
    """一次联网检索的结果；ok=False 时 error_code 说明为什么不可用。

    `answer` 只在百炼这类"搜索 + 生成一次完成"的供应商里非空（已带搜索依据），
    其他供应商留空，由上层用检索片段二次生成。
    """

    ok: bool
    items: list[dict] = field(default_factory=list)
    error_code: Optional[str] = None
    message: str = ""
    answer: str = ""


def _cfg(key: str, default):
    """读运行时配置（配置中心覆盖 > .env > 代码默认）。

    用同步读缓存而不是 get_value：请求链路里上游已经 refresh 过缓存，
    这里保持同步、不额外打库，避免搜索前的额外延迟。
    """
    return app_config.runtime_value(key, default)


def provider() -> str:
    return str(_cfg("WEB_SEARCH_PROVIDER", settings.WEB_SEARCH_PROVIDER)).strip().lower()


def base_url() -> str:
    value = str(_cfg("WEB_SEARCH_BASE_URL", settings.WEB_SEARCH_BASE_URL) or "").strip()
    return (value or DEFAULT_BASE.get(provider(), "")).rstrip("/")


def model() -> str:
    value = str(_cfg("WEB_SEARCH_MODEL", settings.WEB_SEARCH_MODEL) or "").strip()
    return value or DEFAULT_MODEL.get(provider(), "")


def web_enabled() -> bool:
    return bool(_cfg("WEB_SEARCH_ENABLED", settings.WEB_SEARCH_ENABLED))


def timeout_seconds() -> float:
    return float(_cfg("WEB_SEARCH_TIMEOUT", settings.WEB_SEARCH_TIMEOUT))


def max_results() -> int:
    return max(1, min(int(_cfg("WEB_SEARCH_MAX_RESULTS", settings.WEB_SEARCH_MAX_RESULTS)), 10))


def api_key(name: Optional[str] = None) -> str:
    """按供应商取 Key。

    - bailian：BAILIAN_SEARCH_API_KEY 优先；为空才回落 EMBEDDING_API_KEY（同属 DashScope 账号）；
    - bocha / tavily：WEB_SEARCH_API_KEY；只有显式开 REUSE 时才借用 LLM Key。
    """
    name = name or provider()
    if name == "bailian":
        key = (settings.BAILIAN_SEARCH_API_KEY or "").strip()
        return key or (settings.EMBEDDING_API_KEY or "").strip()
    key = (settings.WEB_SEARCH_API_KEY or "").strip()
    if key:
        return key
    if settings.WEB_SEARCH_REUSE_LLM_KEY:
        return (settings.LLM_API_KEY or "").strip()
    return ""


def is_ready(enabled: bool | None = None, name: Optional[str] = None) -> bool:
    """能力是否"看起来可用"（配置层判断，不发起真实请求）。

    enabled / name 显式传入时以调用方读到的运行时配置为准（配置中心可热开关、热切换供应商）。
    """
    name = (name or provider()).strip().lower()
    flag = web_enabled() if enabled is None else bool(enabled)
    key = api_key(name)
    if name == "bailian":
        # 百炼要求模型名非空（否则无法发起联网问答）
        return bool(flag and key and name in PROVIDER_LABELS and model())
    return bool(flag and key and name in PROVIDER_LABELS)


def _normalize_bocha(payload: dict, limit: int) -> list[dict]:
    pages = (((payload or {}).get("data") or {}).get("webPages") or {}).get("value") or []
    return _items_from_rows(
        pages,
        limit,
        title_key="name",
        url_key="url",
        snippet_keys=("summary", "snippet"),
        site_key="siteName",
    )


def _normalize_tavily(payload: dict, limit: int) -> list[dict]:
    rows = (payload or {}).get("results") or []
    return _items_from_rows(
        rows,
        limit,
        title_key="title",
        url_key="url",
        snippet_keys=("content",),
        site_key=None,
    )


def _normalize_bailian(payload: dict, limit: int) -> list[dict]:
    """百炼返回 output.search_info.search_results[]（DashScope 协议专属）。"""
    output = (payload or {}).get("output") or {}
    info = output.get("search_info") or {}
    rows = info.get("search_results") or []
    return _items_from_rows(
        rows,
        limit,
        title_key="title",
        url_key="url",
        snippet_keys=("snippet", "summary", "content"),
        site_key="site_name",
    )


def _items_from_rows(rows, limit, title_key, url_key, snippet_keys, site_key) -> list[dict]:
    items: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        url = (row.get(url_key) or "").strip()
        if not url:
            continue
        title = (row.get(title_key) or url)[:120]
        snippet = ""
        for k in snippet_keys:
            if row.get(k):
                snippet = str(row[k])[:300]
                break
        items.append(
            {
                "title": title,
                "url": url,
                "snippet": snippet,
                "source_type": "web",
                "site": (row.get(site_key) or "") if site_key else "",
            }
        )
        if len(items) >= limit:
            break
    return items


def _bailian_request(query: str) -> tuple[str, dict, dict]:
    """百炼：一次调用同时联网搜索 + 生成答案，并强制返回搜索来源。"""
    name = provider()
    key = api_key(name)
    limit = max_results()
    body = {
        "model": model(),
        "input": {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是企业内部问答系统的联网兜底模块。只依据本次联网检索到的网页作答；"
                        "开头不要重复“以下信息来自网络”这类免责声明；"
                        "引用处用 [1][2] 标注；不确定就直说“检索到的信息不足以回答”；"
                        "不要编造链接、数字或机构名；控制在 5 条要点以内。"
                    ),
                },
                {"role": "user", "content": query},
            ]
        },
        "parameters": {
            "enable_search": True,
            "search_options": {
                # 强制联网：兜底场景就是要外部信息，不能让模型"自己决定不搜"
                "forced_search": True,
                # 必须开启才能拿到 search_info，来源卡片才有 URL 可展示
                "enable_source": True,
                "search_strategy": str(
                    _cfg("WEB_SEARCH_STRATEGY", getattr(settings, "WEB_SEARCH_STRATEGY", "turbo"))
                ),
            },
            "result_format": "message",
        },
    }
    return (
        f"{base_url()}/services/aigc/text-generation/generation",
        body,
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )


def _bocha_request(query: str) -> tuple[str, dict, dict]:
    name = provider()
    key = api_key(name)
    limit = max_results()
    return (
        f"{base_url()}/web-search",
        {"query": query, "count": limit, "summary": True, "freshness": "noLimit"},
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )


def _tavily_request(query: str) -> tuple[str, dict, dict]:
    name = provider()
    key = api_key(name)
    limit = max_results()
    return (
        f"{base_url()}/search",
        {"api_key": key, "query": query, "max_results": limit, "search_depth": "basic"},
        {"Content-Type": "application/json"},
    )


_BUILDERS = {
    "bailian": _bailian_request,
    "bocha": _bocha_request,
    "tavily": _tavily_request,
}


async def ask(query: str) -> WebResult:
    """执行一次联网检索；永远返回结果对象，不抛异常。"""
    q = (query or "").strip()
    name = provider()
    if not web_enabled():
        return WebResult(False, [], ERR_DISABLED, "联网检索未开启")
    if name not in PROVIDER_LABELS:
        return WebResult(False, [], ERR_DISABLED, f"不支持的搜索供应商：{name}")
    if not api_key(name):
        return WebResult(False, [], ERR_NO_KEY, "未配置联网检索 Key")
    if name == "bailian" and not model():
        return WebResult(False, [], ERR_NO_KEY, "未配置联网检索模型")
    if not q:
        return WebResult(False, [], ERR_EMPTY, "检索词为空")

    url, body, headers = _BUILDERS[name](q)
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds()) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.TimeoutException:
        logger.warning("联网检索超时（%ss，供应商 %s）", timeout_seconds(), name)
        return WebResult(False, [], ERR_TIMEOUT, "联网检索超时")
    except Exception as exc:  # noqa: BLE001 - 网络/解析异常都按"不可用"处理
        logger.warning("联网检索请求失败（%s）：%s", name, exc)
        return WebResult(False, [], ERR_HTTP, "联网检索请求失败")

    if resp.status_code != 200:
        logger.warning("联网检索返回 %s（%s）：%s", resp.status_code, name, (resp.text or "")[:200])
        return WebResult(False, [], ERR_HTTP, f"联网检索返回 {resp.status_code}")

    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return WebResult(False, [], ERR_HTTP, "联网检索返回内容无法解析")

    limit = max_results()
    if name == "bailian":
        items = _normalize_bailian(payload, limit)
        answer = _bailian_answer(payload)
    elif name == "tavily":
        items, answer = _normalize_tavily(payload, limit), ""
    else:
        items, answer = _normalize_bocha(payload, limit), ""

    if not items:
        # 没有来源就不算"联网成功"：宁可回落联系管理员，也不用模型自答冒充联网
        return WebResult(False, [], ERR_EMPTY, "联网检索没有返回结果")
    return WebResult(True, items, answer=answer)


def _bailian_answer(payload: dict) -> str:
    """取百炼的答案文本：result_format=message 走 output.choices，兼容默认 output.text。"""
    output = (payload or {}).get("output") or {}
    choices = output.get("choices") or []
    if choices:
        message = (choices[0] or {}).get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):  # 多模态返回为分段列表
            return "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            ).strip()
    text = output.get("text")
    return text.strip() if isinstance(text, str) else ""


def describe_failure(code: Optional[str]) -> str:
    """把失败代号翻成给人看的一句话（进 notice，不暴露内部实现细节）。"""
    return {
        ERR_DISABLED: "管理员关闭了联网检索",
        ERR_NO_KEY: "未配置联网检索服务",
        ERR_TIMEOUT: "联网检索超时",
        ERR_HTTP: "联网检索请求失败",
        ERR_EMPTY: "联网检索没有返回结果",
    }.get(code or "", "联网检索不可用")
