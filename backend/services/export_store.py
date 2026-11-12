"""大结果导出（M5）。

链路：
1. 查询结果超过内联上限时，`biz_query.execute` 调 `create_export()` 生成 CSV；
2. 文件落 `data/exports/`（UTF-8 BOM，Excel 双击可读），库里只存 **token 的 SHA-256**；
3. 下载走 `GET /api/export/{token}`：校验登录用户 + 过期时间（0 = 永不过期）+ 文件是否还在；
4. 链接可在有效期内重复下载（内部工具场景，避免"点一次就废"）；下载只记录 `downloaded_at` 供审计；
5. 清理：启动时与后台每 10 分钟扫一遍，删除**已过期**（仅当设置了有效期）的记录与文件；
   从未被台账登记过的孤儿文件按 24 小时保留窗口清理。

设计取舍：
- 不在下载时重跑 SQL（避免重复计算与并发压力），导出的是查询当时已脱敏的那一份数据；
- 文件名里带查询标题与时间戳，便于用户区分；token 只进 URL，不入日志明文。
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import logging
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..config import settings
from ..db import get_pool
from . import app_config

logger = logging.getLogger("rag.export")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CLEANUP_INTERVAL_SECONDS = 600

_ENSURE_SQL = """
CREATE TABLE IF NOT EXISTS export_token (
    token_hash    CHAR(64) PRIMARY KEY,
    user_id       BIGINT      NOT NULL REFERENCES app_user(user_id) ON DELETE CASCADE,
    file_path     VARCHAR(512) NOT NULL,
    filename      VARCHAR(255) NOT NULL,
    row_count     INT         NOT NULL DEFAULT 0,
    -- NULL = 永不过期
    expires_at    TIMESTAMP   NULL,
    downloaded_at TIMESTAMP   NULL,
    created_at    TIMESTAMP   NOT NULL DEFAULT now()
)
"""


def _export_dir() -> Path:
    return PROJECT_ROOT / settings.EXPORT_DIR_NAME


def _ttl_minutes() -> int:
    try:
        value = app_config.get_cached("BIZ_EXPORT_TTL_MINUTES")
    except Exception:  # noqa: BLE001 - 配置中心不可用时退回 .env
        value = None
    return int(value or settings.BIZ_EXPORT_TTL_MINUTES)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _safe_name(text: str) -> str:
    keep = [ch for ch in (text or "") if ch.isalnum() or ch in ("-", "_", " ")]
    return ("".join(keep).strip() or "biz")[:24]


async def ensure_export_schema() -> None:
    """启动时建表（幂等）；数据库不可用时只记 warning，不影响其它能力。"""
    try:
        pool = get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(_ENSURE_SQL)
                # 老库 expires_at 是 NOT NULL：改为可空，0（永不过期）才能落 NULL
                await conn.execute("ALTER TABLE export_token ALTER COLUMN expires_at DROP NOT NULL")
    except Exception as exc:  # noqa: BLE001
        logger.warning("export_token 建表失败：%s", type(exc).__name__)


def _write_csv(path: Path, columns: list[dict], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([col.get("title") or col.get("key") for col in columns])
        for row in rows:
            writer.writerow(["" if row.get(col["key"]) is None else row.get(col["key"]) for col in columns])


async def create_export(
    user_id: int,
    title: str,
    columns: list[dict],
    rows: list[dict],
) -> Optional[dict]:
    """生成 CSV 与下载令牌；任何异常都返回 None，由调用方降级为“仅内联”。"""
    if not rows:
        return None
    token = secrets.token_urlsafe(32)
    token_hash = _hash_token(token)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"{_safe_name(title)}_{stamp}_{token_hash[:6]}.csv"
    path = _export_dir() / filename
    ttl = _ttl_minutes()
    try:
        _write_csv(path, columns, rows)
        pool = get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO export_token (token_hash, user_id, file_path, filename, row_count, expires_at)
                    VALUES (
                        %(hash)s, %(uid)s, %(path)s, %(name)s, %(count)s,
                        CASE WHEN %(ttl)s::int > 0 THEN now() + (%(ttl)s || ' minutes')::interval ELSE NULL END
                    )
                    """,
                    {
                        "hash": token_hash,
                        "uid": int(user_id),
                        "path": str(path),
                        "name": filename,
                        "count": len(rows),
                        "ttl": str(ttl),
                    },
                )
                cur = await conn.execute(
                    "SELECT expires_at FROM export_token WHERE token_hash = %(hash)s", {"hash": token_hash}
                )
                expires_at = (await cur.fetchone())[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning("导出生成失败：%s", type(exc).__name__)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    return {
        "token": token,
        "url": f"/api/export/{token}",
        "filename": filename,
        "rows": len(rows),
        "expires_at": expires_at.isoformat(sep=" ") if expires_at else None,
        "ttl_minutes": ttl,
    }


async def resolve(token: str, user_id: int) -> tuple[str, Optional[dict]]:
    """校验令牌；返回 (状态, 记录)。状态：ok / forbidden / expired / missing。

    expires_at 为 NULL 表示永不过期；下载次数不设限（只记录 downloaded_at 供审计）。
    """
    token_hash = _hash_token(token)
    try:
        pool = get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    """
                    SELECT token_hash, user_id, file_path, filename, row_count, expires_at, downloaded_at,
                           (expires_at IS NOT NULL AND now() > expires_at) AS is_expired
                    FROM export_token WHERE token_hash = %(hash)s
                    """,
                    {"hash": token_hash},
                )
                row = await cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.warning("导出令牌查询失败：%s", type(exc).__name__)
        return "missing", None

    if row is None:
        return "missing", None
    record = {
        "token_hash": row[0],
        "user_id": row[1],
        "file_path": row[2],
        "filename": row[3],
        "row_count": row[4],
        "expires_at": row[5],
        "downloaded_at": row[6],
        "is_expired": bool(row[7]),
    }
    if int(record["user_id"]) != int(user_id):
        return "forbidden", record
    if record["is_expired"]:
        return "expired", record
    if not Path(record["file_path"]).exists():
        return "missing", record
    return "ok", record


async def mark_downloaded(token_hash: str) -> None:
    try:
        pool = get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE export_token SET downloaded_at = now() WHERE token_hash = %(hash)s",
                    {"hash": token_hash},
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("导出下载标记失败：%s", type(exc).__name__)


async def cleanup_expired() -> int:
    """删除**已过期**（仅设置了有效期）的记录与文件；返回清理条数。

    未设置有效期（expires_at 为 NULL）与已下载过的文件不会被自动删除，
    避免用户拿到的历史链接突然失效。
    """
    removed = 0
    try:
        pool = get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    """
                    DELETE FROM export_token
                    WHERE expires_at IS NOT NULL AND now() > expires_at
                    RETURNING file_path
                    """
                )
                paths = [row[0] for row in await cur.fetchall()]
        for path in paths:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                logger.warning("导出文件删除失败：%s", Path(path).name)
        removed = len(paths)
        if removed:
            logger.info("已清理过期导出 %d 个", removed)
    except Exception as exc:  # noqa: BLE001
        logger.warning("导出台账清理失败：%s", type(exc).__name__)
    # 顺带清掉落盘但台账里没有的孤儿文件（例如建表前写了一半）；保留 24 小时观察窗口
    try:
        directory = _export_dir()
        if directory.exists():
            known = set()
            pool = get_pool()
            async with pool.connection() as conn:
                async with conn.transaction():
                    cur = await conn.execute("SELECT file_path FROM export_token")
                    known = {row[0] for row in await cur.fetchall()}
            cutoff = time.time() - 24 * 3600
            for file in directory.glob("*.csv"):
                if str(file) not in known and file.stat().st_mtime < cutoff:
                    file.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return removed


async def cleanup_loop() -> None:
    """后台清理循环；被取消时静默退出。"""
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            await cleanup_expired()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("导出清理循环异常：%s", type(exc).__name__)


def stats() -> dict[str, Any]:
    """给 /api/system/status 用的轻量信息（不查库，只看目录）。"""
    directory = _export_dir()
    files = list(directory.glob("*.csv")) if directory.exists() else []
    return {"dir": str(directory), "files": len(files)}
