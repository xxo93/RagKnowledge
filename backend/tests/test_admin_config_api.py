"""配置中心 API 级离线回归（不连数据库、不调外部服务）。

覆盖：
1. 未登录访问三个配置接口 → 40101（不能退化成 40001）；
2. member 访问 → 40303；
3. admin 读取 → 分组快照；保存 → 立即生效；非法值 → 40001 且不落库；
4. 恢复默认 → 当前值回到默认。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend.services import account_store, app_config, audit


def _user(role: str = "admin") -> dict:
    return {
        "user_id": 1,
        "username": "admin",
        "display_name": "系统管理员",
        "role": role,
        "status": 1,
    }


@pytest.fixture()
def client() -> TestClient:
    # 不进入 with 上下文：不触发 lifespan，因此不会连接数据库
    return TestClient(main.app)


@pytest.fixture()
def store(monkeypatch):
    rows: dict[str, object] = {}

    async def fake_load():
        return dict(rows)

    async def fake_upsert(items, actor_user_id=None):
        for spec, value in items:
            rows[spec.key] = value

    async def fake_delete(keys):
        for key in list(keys):
            rows.pop(key, None)

    async def fake_audit(*args, **kwargs):
        return None

    monkeypatch.setattr(app_config, "_load_from_db", fake_load)
    monkeypatch.setattr(app_config, "_upsert_rows", fake_upsert)
    monkeypatch.setattr(app_config, "_delete_rows", fake_delete)
    monkeypatch.setattr(audit, "log_operation", fake_audit)
    monkeypatch.setattr(app_config, "_cache", {})
    monkeypatch.setattr(app_config, "_overridden", set())
    monkeypatch.setattr(app_config, "_cache_loaded_at", 0.0)
    monkeypatch.setattr(app_config, "_cache_loaded", False)
    return rows


def _login_as(monkeypatch, role: str) -> None:
    user = _user(role)

    async def fake_get_active_session(token_hash, renew_ttl_seconds=0):
        return user

    monkeypatch.setattr(account_store, "get_active_session", fake_get_active_session)


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/admin/config"),
        ("put", "/api/admin/config"),
        ("post", "/api/admin/config/reset"),
    ],
)
def test_requires_login(client: TestClient, method: str, path: str) -> None:
    resp = getattr(client, method)(path)
    assert resp.status_code == 401
    assert resp.json()["code"] == 40101


def test_member_forbidden(client: TestClient, monkeypatch, store) -> None:
    _login_as(monkeypatch, "member")
    client.cookies.set("rag_session", "dummy-token")
    resp = client.get("/api/admin/config")
    assert resp.status_code == 403
    assert resp.json()["code"] == 40303


def test_admin_reads_grouped_snapshot(client: TestClient, monkeypatch, store) -> None:
    _login_as(monkeypatch, "admin")
    client.cookies.set("rag_session", "dummy-token")
    resp = client.get("/api/admin/config")
    assert resp.status_code == 200
    data = resp.json()["data"]
    groups = {g["key"] for g in data["groups"]}
    assert groups == {"retrieval", "generation", "general", "memory", "session", "quota", "upload", "fallback", "biz", "logs", "workflow"}
    assert data["priority"] == "app_config > .env > 代码默认"

    params = {p["key"]: p for g in data["groups"] for p in g["params"]}
    assert params["RETRIEVAL_MIN_SCORE"]["current"] == 0.5
    assert params["TOP_K"]["max"] == 10
    assert params["RERANK_ENABLED"]["type"] == "bool"
    # 上传组（M2）参数同样出现在快照里，且带范围说明
    assert params["UPLOAD_DEFAULT_CONCURRENCY"]["max"] == 6
    assert params["UPLOAD_TOTAL_MAX_MB"]["current"] == 100


def test_admin_save_upload_cross_field_rejected(client: TestClient, monkeypatch, store) -> None:
    """单片上限超过单文件总上限 → 40001，且整批不落库。"""
    _login_as(monkeypatch, "admin")
    client.cookies.set("rag_session", "dummy-token")
    resp = client.put("/api/admin/config", json={"values": {"UPLOAD_CHUNK_MAX_MB": 300}})
    assert resp.status_code == 400
    assert resp.json()["code"] == 40001
    assert store == {}


def test_system_status_visible_to_member(client: TestClient, monkeypatch, store) -> None:
    """顶栏能力状态灯：member 也能读到布尔能力开关（/info 仍然是 admin only）。"""
    _login_as(monkeypatch, "member")
    client.cookies.set("rag_session", "dummy-token")
    resp = client.get("/api/system/status")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert set(data) == {
        "knowledge_base_ready",
        "llm_ready",
        "web_search_ready",
        "biz_query_ready",
        "fallback_mode",
        "role",
    }
    assert data["role"] == "member"
    # 联网灯跟着"当前供应商有没有 Key"走（本机 .env 已配百炼 Key 时为 True；
    # 具体取值由单测 test_web_search.py 覆盖，这里只校验类型与语义方向）
    assert isinstance(data["web_search_ready"], bool)
    assert data["fallback_mode"] in {"web", "guide", "contact_admin"}


def test_system_status_requires_login(client: TestClient, store) -> None:
    resp = client.get("/api/system/status")
    assert resp.status_code == 401
    assert resp.json()["code"] == 40101


def test_admin_save_takes_effect(client: TestClient, monkeypatch, store) -> None:
    _login_as(monkeypatch, "admin")
    client.cookies.set("rag_session", "dummy-token")

    resp = client.put("/api/admin/config", json={"values": {"TOP_K": 7, "LLM_TEMPERATURE": 0.8}})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["changed"] == ["LLM_TEMPERATURE", "TOP_K"]
    assert store == {"TOP_K": 7, "LLM_TEMPERATURE": 0.8}

    # 同一进程内再次读取即看到新值（热生效）
    snap = client.get("/api/admin/config").json()["data"]
    params = {p["key"]: p for g in snap["groups"] for p in g["params"]}
    assert params["TOP_K"]["current"] == 7
    assert params["TOP_K"]["overridden"] is True
    assert params["LLM_TEMPERATURE"]["current"] == pytest.approx(0.8)


@pytest.mark.parametrize(
    "values",
    [
        {"TOP_K": 99},
        {"RETRIEVAL_MIN_SCORE": -1},
        {"RERANK_ENABLED": "yes"},
        {"NOT_A_PARAM": 1},
    ],
)
def test_admin_save_rejects_invalid(client: TestClient, monkeypatch, store, values) -> None:
    _login_as(monkeypatch, "admin")
    client.cookies.set("rag_session", "dummy-token")
    resp = client.put("/api/admin/config", json={"values": values})
    assert resp.status_code == 400
    assert resp.json()["code"] == 40001
    assert store == {}


def test_admin_reset_restores_default(client: TestClient, monkeypatch, store) -> None:
    _login_as(monkeypatch, "admin")
    client.cookies.set("rag_session", "dummy-token")
    client.put("/api/admin/config", json={"values": {"TOP_K": 9}})
    assert store["TOP_K"] == 9

    resp = client.post("/api/admin/config/reset", json={"keys": ["TOP_K"]})
    assert resp.status_code == 200
    assert store == {}
    snap = client.get("/api/admin/config").json()["data"]
    params = {p["key"]: p for g in snap["groups"] for p in g["params"]}
    assert params["TOP_K"]["current"] == 5
