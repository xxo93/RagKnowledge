"""会话记忆 API 离线回归（不连数据库、不调模型）。

重点验证**会话隔离**：记忆接口不接收 user_id，越权统一 40402；同时校验
OpenAPI 里不存在任何 user_id 查询参数，防止后续改动引入越权后门。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend.services import chat_store, memory, session_memory_store
from backend.services.auth_deps import get_current_user


def _user(user_id: int = 1) -> dict:
    return {"user_id": user_id, "username": "u", "display_name": "U", "role": "member", "status": 1}


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    app = main.app
    app.dependency_overrides[get_current_user] = lambda: _user(1)
    return TestClient(app)


@pytest.fixture(autouse=True)
def cleanup_overrides():
    yield
    main.app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture()
def stores(monkeypatch):
    state = {
        "session_id": 5,
        "session_name": "s",
        "summary": "已有摘要",
        "summarized_until_record_id": 3,
        "memory_enabled": True,
    }
    facts = [
        {"mem_id": 11, "kind": "fact", "fact_key": "金额单位", "fact_value": "万元",
         "confidence": 0.9, "source_record_id": 3, "create_time": None, "update_time": None}
    ]

    async def fake_state(session_id, user_id):
        return dict(state) if session_id == 5 and user_id == 1 else None

    async def fake_set_enabled(session_id, user_id, enabled):
        if session_id != 5 or user_id != 1:
            return None
        state["memory_enabled"] = enabled
        return dict(state)

    async def fake_list_facts(session_id, user_id, limit=20):
        return list(facts) if session_id == 5 and user_id == 1 else []

    async def fake_invalidate(session_id, user_id, mem_id):
        return session_id == 5 and user_id == 1 and mem_id == 11

    async def fake_history(session_id, user_id, mem_id):
        if session_id != 5 or user_id != 1 or mem_id != 11:
            return None
        return [
            {"history_id": 1, "session_id": 5, "fact_id": 11, "fact_key": "金额单位",
             "old_value": "元", "new_value": "万元", "changed_at": None,
             "source_record_id": 3, "created_at": None}
        ]

    async def fake_clear(session_id, user_id):
        return 1 if session_id == 5 and user_id == 1 else 0

    async def fake_reset(session_id, user_id):
        return session_id == 5 and user_id == 1

    async def fake_build(session_id, user_id):
        if session_id != 5 or user_id != 1:
            return memory.ContextBundle(usage=memory._usage_empty())
        return memory.ContextBundle(
            facts=facts,
            summary="已有摘要",
            history=[{"role": "user", "content": "q"}],
            usage={**memory._usage_empty(), "memory_enabled": True, "history_rounds": 1},
        )

    monkeypatch.setattr(chat_store, "get_memory_state", fake_state)
    monkeypatch.setattr(chat_store, "set_memory_enabled", fake_set_enabled)
    monkeypatch.setattr(chat_store, "reset_memory", fake_reset)
    monkeypatch.setattr(session_memory_store, "list_facts", fake_list_facts)
    monkeypatch.setattr(session_memory_store, "invalidate_fact", fake_invalidate)
    monkeypatch.setattr(session_memory_store, "list_fact_history", fake_history)
    monkeypatch.setattr(session_memory_store, "clear_facts", fake_clear)
    monkeypatch.setattr(memory, "build_context", fake_build)
    # 接口内部用的 build_context 是 routers/session_memory 里的引用
    from backend.routers import session_memory as router_mod
    monkeypatch.setattr(router_mod, "memory", memory)
    return state


def test_get_memory_returns_summary_facts_and_usage(client, stores):
    resp = client.get("/api/sessions/5/memory")
    assert resp.status_code == 200
    data = resp.json()
    assert data["code"] == 0
    assert data["data"]["summary"] == "已有摘要"
    assert data["data"]["facts"][0]["fact_key"] == "金额单位"
    assert data["data"]["context_usage"]["memory_enabled"] is True


def test_get_other_user_session_is_40402(client, monkeypatch):
    async def none_state(session_id, user_id):
        return None

    monkeypatch.setattr(chat_store, "get_memory_state", none_state)
    resp = client.get("/api/sessions/999/memory")
    assert resp.status_code == 404
    assert resp.json()["code"] == 40402


def test_delete_fact_and_foreign_mem_id(client, stores):
    resp = client.delete("/api/sessions/5/memory/facts/11")
    assert resp.status_code == 200
    assert resp.json()["code"] == 0

    resp = client.delete("/api/sessions/5/memory/facts/12")
    assert resp.status_code == 404
    assert resp.json()["code"] == 40402


def test_fact_history_returns_versions_and_404_for_foreign(client, stores):
    resp = client.get("/api/sessions/5/memory/11/history")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["fact_id"] == 11
    assert data["history"][0]["old_value"] == "元"
    assert data["history"][0]["new_value"] == "万元"

    resp = client.get("/api/sessions/5/memory/12/history")
    assert resp.status_code == 404
    assert resp.json()["code"] == 40402


def test_update_settings_toggles_temp_session(client, stores):
    resp = client.put("/api/sessions/5/memory/settings", json={"memory_enabled": False})
    assert resp.status_code == 200
    assert resp.json()["data"]["memory_enabled"] is False
    assert stores["memory_enabled"] is False


def test_clear_memory_clears_facts_and_summary(client, stores):
    resp = client.delete("/api/sessions/5/memory")
    assert resp.status_code == 200
    assert resp.json()["data"]["removed_facts"] == 1
    assert resp.json()["data"]["summary_cleared"] is True


def test_memory_openapi_has_no_user_id_parameter():
    spec = main.app.openapi()
    for path, item in spec["paths"].items():
        if "/memory" not in path:
            continue
        for method, op in item.items():
            params = op.get("parameters") or []
            for p in params:
                assert p["name"] != "user_id", f"{method.upper()} {path} 不允许接收 user_id"
