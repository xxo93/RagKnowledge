"""业务库查询（M4）单测：注册表校验、五层闸门、脱敏、限流、截断。

不连真实库：执行层用 monkeypatch 替换 `_fetch`，其余逻辑（注册表/参数/权限/合规/脱敏）全走真实代码。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from backend.services import biz_query


@pytest.fixture(autouse=True)
def reset_state():
    biz_query.reset_rate_limit()
    biz_query._registry_cache = None
    biz_query._registry_mtime = -1.0
    yield
    biz_query.reset_rate_limit()


def test_registry_loads_seven_intents():
    reg = biz_query.load_registry(force=True)
    ids = set(reg["intents"])
    assert ids == {
        "order_by_customer", "inventory_by_sku", "customer_profile",
        "employee_attendance", "payroll_by_employee",
        "customer_orders_by_employee", "recent_orders",
    }
    order = reg["intents"]["order_by_customer"]
    assert order["schema"] == "biz_a"
    assert "customer_name" in {p["name"] for p in order["params"]}
    cross = reg["intents"]["customer_orders_by_employee"]
    assert cross["schema"] == "biz_b"
    assert set(cross["schemas"]) == {"biz_a", "biz_b"}
    assert cross["allowed_roles"] == ["admin"]


def test_registry_rejects_unregistered_table(tmp_path, monkeypatch):
    bad = {
        "version": 1,
        "intents": [
            {
                "intent_id": "bad", "title": "坏", "examples": ["x"], "schema": "biz_a",
                "tables": ["customer"],
                "sql": "SELECT a FROM biz_a.app_user WHERE 1 LIMIT %(limit)s",
                "params": [{"name": "customer_name", "type": "string", "required": True}],
                "allowed_roles": ["admin"], "max_rows": 10,
                "columns": [{"key": "a", "title": "A"}],
            }
        ],
    }
    path = tmp_path / "biz_intents.json"
    path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(biz_query, "_INTENTS_FILE", path)
    with pytest.raises(ValueError):
        biz_query.load_registry(force=True)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM biz_a.customer LIMIT 1",
        "SELECT 1; DROP TABLE biz_a.orders",
        "SELECT name FROM biz_a.customer -- comment",
        "SELECT n FROM information_schema.tables",
        "SELECT name FROM biz_a.customer UNION SELECT name FROM biz_b.employee",
        "DELETE FROM biz_a.orders WHERE 1",
        "SELECT a /* inline */ FROM biz_a.customer",
    ],
)
def test_guard_blocks_illegal_sql(sql):
    intent = {"tables": ["customer"], "columns": [{"key": "name", "title": "姓名"}]}
    assert biz_query.guard_sql(sql, intent) is not None


def test_guard_allows_registry_sql():
    for intent in biz_query.list_intents():
        full = biz_query.get_intent(intent["intent_id"])
        assert biz_query.guard_sql(full["sql"], full) is None


def test_normalize_args_defaults_and_required():
    intent = biz_query.get_intent("order_by_customer")
    clean, problems = biz_query.normalize_args(intent, {"customer_name": "张三"})
    assert problems == []
    assert clean == {"customer_name": "张三", "since_days": 90}

    clean, problems = biz_query.normalize_args(intent, {})
    assert "缺少customer_name" in problems

    clean, problems = biz_query.normalize_args(intent, {"customer_name": "张三", "since_days": "abc"})
    assert any("since_days" in p for p in problems)

    clean, problems = biz_query.normalize_args(intent, {"customer_name": "张三", "since_days": 400})
    assert any("不能大于" in p for p in problems)


@pytest.mark.parametrize(
    "kind,value,expected",
    [
        ("phone", "13812345678", "138****5678"),
        ("id_card", "440101199001011234", "440101********1234"),
        ("email", "zhangsan@example.com", "zh***@example.com"),
        ("name", "张三丰", "张**"),
        ("name", "李四", "李*"),
        ("phone", "", ""),
    ],
)
def test_masking(kind, value, expected):
    assert biz_query.mask_value(kind, value) == expected


def test_role_gate_and_self_scope():
    attendance = biz_query.get_intent("employee_attendance")
    assert biz_query.role_allows(attendance, "admin")
    assert biz_query.role_allows(attendance, "member")
    assert biz_query.use_self_scope(attendance, "member") is True
    assert biz_query.use_self_scope(attendance, "admin") is False

    order = biz_query.get_intent("order_by_customer")
    assert biz_query.role_allows(order, "admin")
    # 客户订单/客户资料含 PII，只对管理员开放；member 只能走本人考勤/薪资
    assert biz_query.role_allows(order, "member") is False
    assert biz_query.use_self_scope(order, "member") is False


def test_member_self_sql_binds_user_id_not_name():
    attendance = biz_query.get_intent("employee_attendance")
    sql, params, problems = biz_query._build_sql(
        attendance, {"employee_name": "别人", "since_days": 30}, {"user_id": 48, "role": "member"}
    )
    assert problems == []
    assert params["user_id"] == 48
    assert "user_id" in sql
    assert "%(employee_name)s" not in sql


def test_missing_locator_rejected_for_admin_scope():
    attendance = biz_query.get_intent("employee_attendance")
    _, _, problems = biz_query._build_sql(attendance, {}, {"user_id": 1, "role": "admin"})
    assert any("缺少查询对象" in p for p in problems)


def test_rate_limit():
    assert biz_query.rate_limit_ok(1, per_minute=3) is True
    assert biz_query.rate_limit_ok(1, per_minute=3) is True
    assert biz_query.rate_limit_ok(1, per_minute=3) is True
    assert biz_query.rate_limit_ok(1, per_minute=3) is False
    biz_query.reset_rate_limit(1)
    assert biz_query.rate_limit_ok(1, per_minute=3) is True


def test_execute_masks_and_limits_rows(monkeypatch):
    fake_rows = [
        {"order_no": "SO1", "customer_name": "张三", "customer_phone": "13812345678",
         "product_name": "商品", "qty": 1, "amount": 100, "status": "paid", "created_at": "2026-10-01"}
    ]

    async def fake_fetch(sql, params, timeout):
        return fake_rows, None

    monkeypatch.setattr(biz_query, "_fetch", fake_fetch)
    result = asyncio.run(biz_query.execute("order_by_customer", {"customer_name": "张三"}, {"user_id": 1, "role": "admin"}))
    assert result["ok"] is True
    table = result["table"]
    assert table["total"] == 1
    assert table["truncated"] is False
    row = table["rows"][0]
    assert row["customer_phone"] == "138****5678"
    assert row["customer_name"] == "张*"
    assert table["download_url"] is None


def test_execute_marks_truncated_with_count(monkeypatch):
    calls = {"count": 0}

    async def fake_fetch(sql, params, timeout):
        if sql.startswith("SELECT COUNT(*)"):
            return [{"n": 350}], None
        return [{"order_no": f"SO{i}"} for i in range(int(params["limit"]))], None

    monkeypatch.setattr(biz_query, "_fetch", fake_fetch)
    result = asyncio.run(biz_query.execute("order_by_customer", {"customer_name": "张三"}, {"user_id": 1, "role": "admin"}))
    table = result["table"]
    assert table["truncated"] is True
    assert table["total"] == 350
    assert len(table["rows"]) == table["inline_max"]


def test_execute_blocks_when_compliance_guard_trips(monkeypatch):
    async def fake_fetch(sql, params, timeout):  # pragma: no cover - 不应执行到
        raise AssertionError("合规拦截时不应执行 SQL")

    monkeypatch.setattr(biz_query, "_fetch", fake_fetch)
    # 直接把注册表里的 SQL 换成非法语句，模拟"有人改了注册表"
    monkeypatch.setitem(biz_query.load_registry(force=True)["intents"], "order_by_customer", {
        **biz_query.get_intent("order_by_customer"),
        "sql": "SELECT name FROM biz_a.customer; DROP TABLE biz_a.orders",
    })
    result = asyncio.run(biz_query.execute("order_by_customer", {"customer_name": "张三"}, {"user_id": 1, "role": "admin"}))
    assert result["ok"] is False
    assert result["code"] == "BIZ_ILLEGAL_SQL"


def test_execute_reports_clarify_without_sql(monkeypatch):
    async def fake_fetch(sql, params, timeout):  # pragma: no cover
        raise AssertionError("缺参数时不应执行 SQL")

    monkeypatch.setattr(biz_query, "_fetch", fake_fetch)
    result = asyncio.run(biz_query.execute("order_by_customer", {}, {"user_id": 1, "role": "admin"}))
    assert result["ok"] is False
    assert result["code"] == "BIZ_NEED_CLARIFY"
    assert "缺少customer_name" in result["message"]


def test_recognize_keyword_fallback(monkeypatch):
    monkeypatch.setattr(biz_query, "llm_recognize", lambda q, c: None)
    hit = biz_query.recognize("查客户张三的订单")
    assert hit["intent"] == "order_by_customer"
    assert hit["args"].get("customer_name") == "张三"
    assert hit["source"] == "keyword"

    sku = biz_query.recognize("查一下库存 P001")
    assert sku["intent"] == "inventory_by_sku"
    assert sku["args"].get("sku") == "P001"


def test_decide_and_execute_low_confidence_clarifies(monkeypatch):
    monkeypatch.setattr(biz_query, "recognize", lambda q, user=None: {"intent": "order_by_customer", "args": {}, "confidence": 0.3, "reason": "", "source": "llm"})
    result = asyncio.run(biz_query.decide_and_execute("查一下那个东西", {"user_id": 1, "role": "admin"}))
    assert result["code"] == "BIZ_NEED_CLARIFY"


def test_decide_and_execute_no_intent_gives_capability_list(monkeypatch):
    monkeypatch.setattr(biz_query, "recognize", lambda q, user=None: {"intent": None, "args": {}, "confidence": 0.0, "reason": "", "source": "keyword"})
    result = asyncio.run(biz_query.decide_and_execute("今天天气怎么样", {"user_id": 1, "role": "admin"}))
    assert result["code"] == "BIZ_NO_INTENT"
    assert "按客户查订单" in result["message"]


def test_decide_and_execute_denies_role(monkeypatch):
    monkeypatch.setattr(biz_query, "recognize", lambda q, user=None: {"intent": "payroll_by_employee", "args": {"employee_name": "张三"}, "confidence": 0.9, "reason": "", "source": "llm"})
    monkeypatch.setattr(biz_query, "role_allows", lambda intent, role: False)
    result = asyncio.run(biz_query.decide_and_execute("查张三的工资", {"user_id": 48, "role": "member"}))
    assert result["code"] == "BIZ_FORBIDDEN"


def test_biz_query_ready_flag():
    assert biz_query.is_ready() is True


# ---------------- M5：跨 schema 校验 / COUNT 改写 / 导出触发 ----------------
def test_count_sql_strips_order_and_limit():
    sql = (
        "SELECT o.order_no, c.name AS customer_name FROM biz_a.orders o "
        "JOIN biz_a.customer c ON c.id = o.customer_id "
        "WHERE o.created_at >= (CURRENT_DATE - %(since_days)s::int) ORDER BY o.created_at DESC LIMIT %(limit)s"
    )
    count = biz_query._count_sql(sql)
    assert count.startswith("SELECT COUNT(*) AS n FROM biz_a.orders o")
    assert "%(limit)s" not in count
    assert "ORDER BY" not in count
    assert "%(since_days)s" in count


def test_registry_rejects_undeclared_schema(tmp_path, monkeypatch):
    bad = {
        "version": 1,
        "intents": [
            {
                "intent_id": "bad", "title": "坏", "examples": ["x"], "schema": "biz_a",
                "schemas": ["biz_a"],
                "tables": ["customer", "employee"],
                "sql": "SELECT c.name FROM biz_a.customer c JOIN biz_b.employee e ON e.id = c.owner_employee_id WHERE 1 LIMIT %(limit)s",
                "params": [{"name": "customer_name", "type": "string", "required": True}],
                "allowed_roles": ["admin"], "max_rows": 10,
                "columns": [{"key": "name", "title": "姓名"}],
            }
        ],
    }
    path = tmp_path / "biz_intents.json"
    path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(biz_query, "_INTENTS_FILE", path)
    with pytest.raises(ValueError, match="未声明的 schema"):
        biz_query.load_registry(force=True)


def test_guard_blocks_undeclared_schema():
    intent = {
        "schema": "biz_a",
        "schemas": ["biz_a"],
        "tables": ["customer", "employee"],
        "sql": "",
    }
    reason = biz_query.guard_sql(
        "SELECT c.name FROM biz_a.customer c JOIN biz_b.employee e ON e.id = c.owner_employee_id LIMIT 10", intent
    )
    assert reason and "schema" in reason


def test_cross_schema_intent_passes_guard():
    intent = biz_query.get_intent("customer_orders_by_employee")
    reason = biz_query.guard_sql(intent["sql"], intent)
    assert reason is None


def test_execute_creates_export_when_over_inline(monkeypatch):
    """超过内联上限时生成一次性导出，表格只保留内联行数并带 download_url。"""
    calls: dict[str, Any] = {}

    async def fake_fetch(sql, params, timeout):
        if "COUNT(*) AS n" in sql:
            return [{"n": 300}], None
        rows = [
            {"order_no": f"SO{i:05d}", "customer_name": "张三", "product_name": "商品", "qty": 1,
             "amount": 10.5, "status": "paid", "created_at": "2026-11-01 10:00:00"}
            for i in range(int(params["limit"]))
        ]
        return rows, None

    async def fake_create_export(user_id, title, columns, rows):
        calls["export"] = {"rows": len(rows), "title": title, "columns": [c["key"] for c in columns]}
        return {"url": "/api/export/token123", "rows": len(rows), "expires_at": "2026-11-11 23:00:00", "ttl_minutes": 30}

    monkeypatch.setattr(biz_query, "_fetch", fake_fetch)
    # create_export 在业务模块里是 export_store 的属性，替换模块属性即可
    monkeypatch.setattr(biz_query.export_store, "create_export", fake_create_export)
    result = asyncio.run(biz_query.execute("recent_orders", {"since_days": 120}, {"user_id": 1, "role": "admin"}))
    assert result["ok"] is True
    table = result["table"]
    assert table["total"] == 300
    assert len(table["rows"]) == table["inline_max"]
    assert table["download_url"] == "/api/export/token123"
    assert table["download_rows"] == calls["export"]["rows"] > table["inline_max"]
    assert table["download_ttl_minutes"] == 30
    assert calls["export"]["title"] == "近 N 天订单列表"
    # 300 行未超硬上限（1000/3000），导出已含全部结果，不应误标 hard_truncated
    assert table["hard_truncated"] is False


def test_execute_marks_hard_truncated_over_hard_cap(monkeypatch):
    """总数超过硬上限时导出只含前 hard_max 行，并显式标记 hard_truncated。"""
    async def fake_fetch(sql, params, timeout):
        if "COUNT(*) AS n" in sql:
            return [{"n": 5000}], None
        return [{"order_no": f"SO{i:05d}", "customer_name": "张三", "product_name": "商品", "qty": 1,
                 "amount": 10.5, "status": "paid", "created_at": "2026-11-01 10:00:00"}
                for i in range(int(params["limit"]))], None

    async def fake_create_export(user_id, title, columns, rows):
        return {"url": "/api/export/t9", "rows": len(rows), "expires_at": "2026-11-11 23:00:00", "ttl_minutes": 30}

    monkeypatch.setattr(biz_query, "_fetch", fake_fetch)
    monkeypatch.setattr(biz_query.export_store, "create_export", fake_create_export)
    result = asyncio.run(biz_query.execute("recent_orders", {"since_days": 120}, {"user_id": 1, "role": "admin"}))
    table = result["table"]
    assert table["total"] == 5000
    assert table["hard_truncated"] is True
    assert table["download_rows"] == 3000


def test_execute_no_export_when_within_inline(monkeypatch):
    async def fake_fetch(sql, params, timeout):
        return [{"order_no": "SO1", "customer_name": "张三", "product_name": "商品", "qty": 1,
                 "amount": 10.5, "status": "paid", "created_at": "2026-11-01 10:00:00"}], None

    async def no_export(*args, **kwargs):
        raise AssertionError("内联范围内不应创建导出")

    monkeypatch.setattr(biz_query, "_fetch", fake_fetch)
    monkeypatch.setattr(biz_query.export_store, "create_export", no_export)
    result = asyncio.run(biz_query.execute("recent_orders", {"since_days": 3}, {"user_id": 1, "role": "admin"}))
    assert result["ok"] is True
    assert result["table"]["download_url"] is None
