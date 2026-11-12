"""统一 LLM 客户端（架构 ADR-009：OpenAI 兼容协议，Provider 可配）。

- 内部使用 openai SDK，通过 base_url 切换 DeepSeek / 豆包 / 通义 / OpenAI；
- 业务层只允许经本模块访问 LLM。
- 缺失密钥、依赖缺失、超时或接口异常统一转换为 BizError，由调用方决定降级策略。
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Optional

from .config import settings
from .errors import BizError, ERR_LLM_UNAVAILABLE

logger = logging.getLogger("rag.llm")

_client = None


def get_client():
    """惰性创建 OpenAI 兼容客户端；未配置 Key 时抛出统一业务错误。"""
    global _client
    if _client is not None:
        return _client
    if not settings.LLM_API_KEY:
        raise BizError(ERR_LLM_UNAVAILABLE, "未配置 LLM_API_KEY，模型服务不可用", http_status=500)
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise BizError(ERR_LLM_UNAVAILABLE, "缺少 openai 依赖，请先安装 requirements.txt") from exc

    _client = OpenAI(
        api_key=settings.LLM_API_KEY,
        base_url=settings.LLM_BASE_URL,
        timeout=settings.LLM_TIMEOUT,
    )
    return _client


def _generation_params(temperature: Optional[float]) -> tuple[float, int]:
    """解析生成参数：显式入参优先，否则读运行时配置（配置中心可在线调）。

    同步函数（chat 在线程里跑）不能 await 数据库，因此只读 app_config 的进程内缓存；
    缓存由检索/配置接口等异步入口按 5s TTL 刷新，保存配置时也会立即刷新。
    """
    try:
        from .services import app_config

        t = app_config.get_cached("LLM_TEMPERATURE")
        max_tokens = app_config.get_cached("LLM_MAX_TOKENS")
    except Exception:  # noqa: BLE001 - 配置读取失败不得影响生成
        t = settings.LLM_TEMPERATURE
        max_tokens = settings.LLM_MAX_TOKENS
    resolved_t = float(t) if temperature is None else float(temperature)
    return resolved_t, int(max_tokens)


def chat(messages: list[dict], *, temperature: Optional[float] = None, json_mode: bool = False) -> str:
    """同步一次性生成；用于意图判断、短文本汇总等不需要 token 流的场景。

    temperature=None 时取配置中心的当前值（默认 0.3），不再硬编码在函数默认参数里。
    """
    client = get_client()
    resolved_t, max_tokens = _generation_params(temperature)
    kwargs: dict = {
        "model": settings.LLM_MODEL,
        "messages": messages,
        "temperature": resolved_t,
        "max_tokens": max_tokens,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


async def chat_stream(messages: list[dict], *, temperature: Optional[float] = None) -> AsyncIterator[str]:
    """流式生成 SSE token。

    OpenAI SDK 迭代是同步阻塞调用；这里在线程中生产内容，经 asyncio.Queue
    桥接为异步生成器。模型错误统一转换为 BizError，避免击穿 SSE 主链路。
    """
    client = get_client()
    resolved_t, max_tokens = _generation_params(temperature)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()

    def _produce() -> None:
        try:
            stream = client.chat.completions.create(
                model=settings.LLM_MODEL,
                messages=messages,
                temperature=resolved_t,
                max_tokens=max_tokens,
                stream=True,
            )
            for chunk in stream:
                if not chunk.choices:
                    continue
                text = getattr(chunk.choices[0].delta, "content", None)
                if text:
                    loop.call_soon_threadsafe(queue.put_nowait, text)
        except Exception as exc:  # noqa: BLE001 - 统一经队列上抛，转业务错误
            loop.call_soon_threadsafe(queue.put_nowait, exc)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, sentinel)

    worker = asyncio.create_task(asyncio.to_thread(_produce))
    try:
        while True:
            item = await queue.get()
            if item is sentinel:
                break
            if isinstance(item, Exception):
                raise BizError(ERR_LLM_UNAVAILABLE, "模型流式生成失败，请稍后重试", http_status=503) from item
            yield item
    finally:
        worker.cancel()
        try:
            await worker
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
