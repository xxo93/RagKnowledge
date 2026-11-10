"""统一 LLM 客户端（架构 ADR-009：OpenAI 兼容协议，Provider 可配）。

- 内部使用 openai SDK，通过 base_url 切换 DeepSeek / 豆包 / 通义 / OpenAI；
- 各家私有参数（如 reasoning_effort、thinking）通过 extra_body 透传，不散落在业务代码；
- 本阶段（骨架）只完成客户端工厂与接口契约，真实流式生成在 M2/M4 接入；
- 业务层只允许经本模块访问 LLM（依赖规则 R6）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Optional

from config import settings
from errors import BizError, ERR_LLM_UNAVAILABLE

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


def chat(messages: list[dict], *, temperature: float = 0.3, json_mode: bool = False) -> str:
    """同步一次性生成（用于意图判断、汇总等短文本）。M2 阶段补全错误映射。"""
    client = get_client()
    kwargs: dict = {
        "model": settings.LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


async def chat_stream(messages: list[dict], *, temperature: float = 0.3) -> AsyncIterator[str]:
    """流式生成（SSE token 事件来源，M4 接通）。

    OpenAI SDK 的 stream 迭代是同步阻塞调用，这里用后台线程生产、asyncio.Queue +
    loop.call_soon_threadsafe 桥接为异步生成器；缺 Key / 超时 / 接口异常统一抛
    BizError(ERR_LLM_UNAVAILABLE)，由调用方（rag.stream_answer / 图路由）降级，不击穿 SSE。
    """
    client = get_client()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()

    def _produce() -> None:
        try:
            stream = client.chat.completions.create(
                model=settings.LLM_MODEL,
                messages=messages,
                temperature=temperature,
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
