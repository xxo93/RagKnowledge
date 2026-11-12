"""导出服务（M5）单测：令牌哈希、文件名、CSV 编码与表头、TTL 读取。

不连库：只覆盖纯函数与文件写盘逻辑。
"""
from __future__ import annotations

import csv
import hashlib
from pathlib import Path

from backend.services import export_store


def test_hash_token_is_sha256():
    token = "abc123"
    assert export_store._hash_token(token) == hashlib.sha256(token.encode()).hexdigest()
    assert len(export_store._hash_token("x")) == 64


def test_safe_name_keeps_chinese_and_trims():
    assert export_store._safe_name("近 N 天订单列表") == "近 N 天订单列表"
    assert export_store._safe_name("a/b:c*d") == "abcd"
    assert export_store._safe_name("") == "biz"


def test_write_csv_has_utf8_bom_and_chinese_header(tmp_path):
    path = tmp_path / "demo.csv"
    columns = [
        {"key": "order_no", "title": "订单号"},
        {"key": "customer_name", "title": "客户"},
        {"key": "amount", "title": "金额"},
    ]
    rows = [
        {"order_no": "SO1", "customer_name": "张*", "amount": 12.5},
        {"order_no": "SO2", "customer_name": "李*", "amount": None},
    ]
    export_store._write_csv(path, columns, rows)

    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    header = next(csv.reader(text.splitlines()))
    assert header == ["订单号", "客户", "金额"]
    assert "张*" in text
    # None 写成空字符串，不留 "None"
    assert "None" not in text


def test_default_ttl_never_expires():
    # 默认 0 = 永不过期（内部工具场景，避免“点一次就废/过一小时就废”）
    assert export_store._ttl_minutes() == 0


def test_export_dir_under_project_root():
    assert export_store._export_dir() == export_store.PROJECT_ROOT / "data/exports"
    assert isinstance(export_store._export_dir(), Path)
