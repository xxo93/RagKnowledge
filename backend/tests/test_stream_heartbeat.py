"""token 期心跳工具离线单测（零外部调用，asyncio.run 驱动，不依赖 pytest-asyncio）。

覆盖：静默期产出心跳哨兵、快速产出不误报心跳、空生成器直接结束、正常结束后关闭底层生成器。
"""
from __future__ import annotations

import asyncio

from backend.services import stream_heartbeat as sh


async def _collect(gen, interval: float):
    out = []
    async for v in sh.aiter_with_heartbeat(gen, interval):
        out.append(v)
    return out


def test_heartbeat_emits_sentinel_when_silent():
    """底层静默超过 interval：先产出 None（心跳信号），token 最终不丢失。"""

    async def gen():
        await asyncio.sleep(0.15)
        yield "A"

    out = asyncio.run(_collect(gen(), 0.05))
    assert None in out          # 至少一次静默心跳
    assert "A" in out           # 底层结果仍完整保留（shield 未取消）


def test_fast_items_produce_no_false_heartbeat():
    """相邻产出都快于 interval：不应误发心跳哨兵。"""

    async def gen():
        yield "X"
        await asyncio.sleep(0.01)
        yield "Y"

    out = asyncio.run(_collect(gen(), 1.0))
    assert out == ["X", "Y"]
    assert None not in out


def test_empty_generator_yields_nothing():
    """空生成器直接结束，不产生心跳。"""

    async def gen():
        if False:  # pragma: no cover - 仅令其成为 async generator
            yield

    out = asyncio.run(_collect(gen(), 0.05))
    assert out == []


def test_generator_closed_after_completion():
    """正常结束后底层生成器被兜底关闭一次。"""
    closed = {"n": 0}

    async def gen():
        try:
            yield "Z"
        finally:
            closed["n"] += 1

    out = asyncio.run(_collect(gen(), 1.0))
    assert out == ["Z"]
    assert closed["n"] == 1
