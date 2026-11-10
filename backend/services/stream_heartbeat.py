"""为异步 token 生成器叠加静默期 SSE 注释帧心跳（零外部调用、纯本地计时）。

背景：流前置准备阶段已有独立心跳；本模块覆盖 token 流式期——LLM 偶发长时间不产出时，
向 SSE 连接发送注释帧 ``: ping``，防止代理/连接因静默过久而断开。

关键约束：``asyncio.wait_for`` 超时只能放弃“本次等待”，绝不能取消底层生成器（取消会
触发 ``llm.chat_stream`` 的 finally 关闭生产 worker，导致后续 token 丢失），因此用
``asyncio.shield`` 复用同一个 pending future，超时后底层仍在后台推进，结果不丢失。
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator, TypeVar

T = TypeVar("T")

#: 心跳信号（与数据项区分；None 不是有效 token，故可安全复用）
HEARTBEAT_SENTINEL: None = None


async def aiter_with_heartbeat(
    aiter: AsyncIterator[T], interval: float
) -> AsyncIterator[T | None]:
    """迭代异步生成器；静默超过 ``interval`` 秒则产出 ``None``（调用方据此发送 ": ping"）。

    - 产出 ``None`` 表示心跳信号而非数据；非 ``None`` 为底层产出项；
    - 底层迭代完整结束后正常退出；退出时兜底关闭底层生成器；
    - 超时不取消底层生成器，pending future 被复用，已完成结果不丢失。
    """
    pending: asyncio.Future | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(aiter.__anext__())
            try:
                item = await asyncio.wait_for(asyncio.shield(pending), timeout=interval)
            except asyncio.TimeoutError:
                yield HEARTBEAT_SENTINEL
                continue
            except StopAsyncIteration:
                break
            pending = None
            yield item
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
        close = getattr(aiter, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception:  # noqa: BLE001 - 关闭阶段吞异常，不影响主链路
                pass
