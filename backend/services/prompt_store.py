"""提示词版本化存储（v3 / WP5-7）。

表结构 `prompt_template`：
- 每个 `name` 有多个版本，`version` 单调递增；
- 同一 `name` 只有一条 `is_active=true`，表示"当前目标版本"；
- `gray_percent` 是当前版本的灰度比例：按 `hash(name:user_id) % 100` 分桶，
  命中灰度的用户走当前版本，其余用户自动回落到上一个版本（没有上一版则仍用当前版）；
- 管理员可随时把任意历史版本设为当前版本（回滚），不需要改代码。

读取侧带 5 秒进程内缓存，管理端写入后立即失效，保证"改完下一条请求生效"。
提示词只从 `prompts.PROMPT_REGISTRY` 登记过的 name 读取，避免出现"能改但不生效"的假配置。
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Optional

from ..db import get_pool
from ..errors import BizError, ERR_PARAM
from ..prompts import PROMPT_REGISTRY
from . import audit

logger = logging.getLogger("rag.prompt_store")

CACHE_TTL_SECONDS = 5.0
_cache: dict[str, tuple[float, list[dict]]] = {}


def _check_name(name: str) -> dict[str, str]:
    meta = PROMPT_REGISTRY.get(name)
    if not meta:
        raise BizError(ERR_PARAM, f"未知提示词：{name}", http_status=400)
    return meta


def bucket(name: str, user_id: Optional[int]) -> int:
    """稳定分桶：同一用户在同一提示词上始终落在同一桶，避免灰度来回跳。"""
    raw = f"{name}:{int(user_id or 0)}".encode("utf-8")
    return int(hashlib.sha1(raw).hexdigest(), 16) % 100


async def ensure_schema() -> None:
    """启动时幂等建表并补齐默认版本；数据库不可用只告警。"""
    try:
        pool = get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS prompt_template (
                    prompt_id    BIGSERIAL PRIMARY KEY,
                    name         VARCHAR(64)  NOT NULL,
                    version      INTEGER      NOT NULL,
                    content      TEXT         NOT NULL,
                    gray_percent SMALLINT     NOT NULL DEFAULT 100,
                    is_active    BOOLEAN      NOT NULL DEFAULT FALSE,
                    created_by   BIGINT       NULL,
                    created_at   TIMESTAMP    NOT NULL DEFAULT now(),
                    updated_at   TIMESTAMP    NOT NULL DEFAULT now(),
                    UNIQUE (name, version)
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_prompt_name_active ON prompt_template(name, is_active)"
            )
            for name, meta in PROMPT_REGISTRY.items():
                cur = await conn.execute(
                    "SELECT COUNT(*) FROM prompt_template WHERE name = %(name)s",
                    {"name": name},
                )
                count = int((await cur.fetchone() or (0,))[0] or 0)
                if count == 0:
                    await conn.execute(
                        """
                        INSERT INTO prompt_template (name, version, content, gray_percent, is_active)
                        VALUES (%(name)s, 1, %(content)s, 100, TRUE)
                        """,
                        {"name": name, "content": meta["default"]},
                    )
        _cache.clear()
    except Exception:  # noqa: BLE001 - 建表失败不阻断启动
        logger.warning("提示词版本表初始化失败，将在首次读取时重试", exc_info=True)


async def _rows(name: str) -> list[dict]:
    now = time.monotonic()
    cached = _cache.get(name)
    if cached and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT version, content, gray_percent, is_active, created_at
            FROM prompt_template
            WHERE name = %(name)s
            ORDER BY version DESC
            """,
            {"name": name},
        )
        rows = await cur.fetchall()
    result = [
        {
            "version": int(r[0]),
            "content": r[1],
            "gray_percent": int(r[2]),
            "is_active": bool(r[3]),
            "created_at": r[4],
        }
        for r in rows
    ]
    _cache[name] = (now, result)
    return result


async def resolve(name: str, user_id: Optional[int] = None) -> dict[str, Any]:
    """按灰度解析出生效提示词；数据库异常时回落到代码默认，保证问答可用。"""
    meta = _check_name(name)
    try:
        rows = await _rows(name)
    except Exception:  # noqa: BLE001 - 提示词存储故障不击穿回答
        logger.warning("提示词版本读取失败，回落到代码默认：%s", name, exc_info=True)
        return {
            "name": name,
            "content": meta["default"],
            "version": None,
            "base_version": None,
            "gray_percent": None,
            "source": "default",
        }

    if not rows:
        return {
            "name": name,
            "content": meta["default"],
            "version": None,
            "base_version": None,
            "gray_percent": None,
            "source": "default",
        }

    current = next((r for r in rows if r["is_active"]), rows[0])
    base = next((r for r in rows if r["version"] < current["version"]), None)
    gray = int(current["gray_percent"])
    if gray >= 100 or base is None or bucket(name, user_id) < gray:
        chosen = current
        source = "current"
    else:
        chosen = base
        source = "base"
    return {
        "name": name,
        "content": chosen["content"],
        "version": chosen["version"],
        "current_version": current["version"],
        "base_version": base["version"] if base else None,
        "gray_percent": gray,
        "source": source,
    }


async def list_prompts() -> list[dict]:
    """管理端列表：每个提示词的当前版本、灰度与全部历史版本。"""
    await ensure_schema()
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT name, version, content, gray_percent, is_active, created_at
            FROM prompt_template
            ORDER BY name, version DESC
            """
        )
        rows = await cur.fetchall()
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(r[0], []).append(
            {
                "version": int(r[1]),
                "content": r[2],
                "gray_percent": int(r[3]),
                "is_active": bool(r[4]),
                "created_at": r[5],
            }
        )
    items = []
    for name, meta in PROMPT_REGISTRY.items():
        versions = grouped.get(name, [])
        current = next((v for v in versions if v["is_active"]), versions[0] if versions else None)
        items.append(
            {
                "name": name,
                "label": meta["label"],
                "desc": meta["desc"],
                "current_version": current["version"] if current else None,
                "gray_percent": current["gray_percent"] if current else 100,
                "versions": versions,
            }
        )
    return items


def _validate_content(content: str) -> str:
    value = (content or "").strip()
    if not value:
        raise BizError(ERR_PARAM, "提示词内容不能为空")
    if len(value) > 8000:
        raise BizError(ERR_PARAM, "提示词内容不能超过 8000 字")
    return value


def _validate_gray(value: int) -> int:
    percent = int(value)
    if percent < 0 or percent > 100:
        raise BizError(ERR_PARAM, "灰度比例必须在 0–100 之间")
    return percent


async def create_version(
    name: str, content: str, gray_percent: int, user_id: Optional[int] = None
) -> dict:
    """新增一个版本并设为当前版本；旧当前版本自动成为灰度回落基线。"""
    _check_name(name)
    content = _validate_content(content)
    gray = _validate_gray(gray_percent)
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM prompt_template WHERE name = %(name)s",
                {"name": name},
            )
            next_version = int((await cur.fetchone() or (0,))[0] or 0) + 1
            await conn.execute(
                "UPDATE prompt_template SET is_active = FALSE, updated_at = now() WHERE name = %(name)s",
                {"name": name},
            )
            await conn.execute(
                """
                INSERT INTO prompt_template (name, version, content, gray_percent, is_active, created_by)
                VALUES (%(name)s, %(version)s, %(content)s, %(gray)s, TRUE, %(created_by)s)
                """,
                {
                    "name": name,
                    "version": next_version,
                    "content": content,
                    "gray": gray,
                    "created_by": user_id,
                },
            )
    _cache.pop(name, None)
    await audit.log_operation(user_id, "prompt_version_create", target_type="prompt", target_id=name, detail={"version": next_version, "gray_percent": gray})
    return {"name": name, "version": next_version, "gray_percent": gray}


async def set_gray(name: str, gray_percent: int, user_id: Optional[int] = None) -> dict:
    """只调整当前版本的灰度，不新增版本。"""
    _check_name(name)
    gray = _validate_gray(gray_percent)
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            UPDATE prompt_template
            SET gray_percent = %(gray)s, updated_at = now()
            WHERE name = %(name)s AND is_active = TRUE
            RETURNING version
            """,
            {"name": name, "gray": gray},
        )
        row = await cur.fetchone()
        if not row:
            raise BizError(ERR_PARAM, "该提示词没有启用中的版本", http_status=400)
        version = int(row[0])
    _cache.pop(name, None)
    await audit.log_operation(user_id, "prompt_gray_update", target_type="prompt", target_id=name, detail={"version": version, "gray_percent": gray})
    return {"name": name, "version": version, "gray_percent": gray}


async def activate(name: str, version: int, gray_percent: int = 100, user_id: Optional[int] = None) -> dict:
    """把指定历史版本设为当前版本；默认 100% 全量，等于立即回滚。"""
    _check_name(name)
    gray = _validate_gray(gray_percent)
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT 1 FROM prompt_template WHERE name = %(name)s AND version = %(version)s",
                {"name": name, "version": int(version)},
            )
            if await cur.fetchone() is None:
                raise BizError(ERR_PARAM, f"版本不存在：v{version}", http_status=400)
            await conn.execute(
                "UPDATE prompt_template SET is_active = FALSE, updated_at = now() WHERE name = %(name)s",
                {"name": name},
            )
            await conn.execute(
                """
                UPDATE prompt_template
                SET is_active = TRUE, gray_percent = %(gray)s, updated_at = now()
                WHERE name = %(name)s AND version = %(version)s
                """,
                {"name": name, "version": int(version), "gray": gray},
            )
    _cache.pop(name, None)
    await audit.log_operation(user_id, "prompt_activate", target_type="prompt", target_id=name, detail={"version": int(version), "gray_percent": gray})
    return {"name": name, "version": int(version), "gray_percent": gray}

