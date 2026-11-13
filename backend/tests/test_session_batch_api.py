"""v4 / N6 批量会话操作 API 离线回归。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend.errors import BizError, ERR_SESSION_NOT_FOUND
from backend.services import chat_store
from backend.services.auth_deps import get_current_user


def _user() -> dict:
    return {"user_id": 1, "username": "u", "display_name": "U", "role": "member", "status": 1}


@pytest.fixture()
def client() -> TestClient:
    main.app.dependency_overrides[get_current_user] = lambda: _user()
    yield TestClient(main.app)
    main.app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture()
def store(monkeypatch):
    valid = {1, 2}

    async def fake_pin(session_id, user_id, pinned):
        if session_id not in valid or user_id != 1:
            raise BizError(ERR_SESSION_NOT_FOUND, "会话不存在或无权访问", http_status=404)
        return {"session_id": session_id, "is_pinned": pinned}

    async def fake_archive(session_id, user_id, archived):
        if session_id not in valid or user_id != 1:
            raise BizError(ERR_SESSION_NOT_FOUND, "会话不存在或无权访问", http_status=404)
        return {"session_id": session_id, "archived_at": "now" if archived else None}

    monkeypatch.setattr(chat_store, "set_session_pinned", fake_pin)
    monkeypatch.setattr(chat_store, "set_session_archived", fake_archive)


def test_batch_mixed_ids_only_succeeds_valid(client, store):
    resp = client.post("/api/chat/session/batch", json={"action": "pin", "session_ids": [1, 3, 2]})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["succeeded"] == [1, 2]
    assert data["failed"][0]["session_id"] == 3
    assert data["failed"][0]["code"] == 40402


def test_batch_archive_and_restore(client, store):
    resp = client.post("/api/chat/session/batch", json={"action": "archive", "session_ids": [1, 2]})
    assert resp.status_code == 200
    assert resp.json()["data"]["succeeded"] == [1, 2]

    resp = client.post("/api/chat/session/batch", json={"action": "restore", "session_ids": [1]})
    assert resp.status_code == 200
    assert resp.json()["data"]["succeeded"] == [1]


def test_batch_rejects_empty_and_too_many(client, store):
    resp = client.post("/api/chat/session/batch", json={"action": "pin", "session_ids": []})
    assert resp.status_code in (400, 422)
    assert resp.json()["code"] == 40001

    resp = client.post("/api/chat/session/batch", json={"action": "pin", "session_ids": list(range(1, 52))})
    assert resp.status_code in (400, 422)
    assert resp.json()["code"] == 40001


def test_batch_rejects_delete_action(client, store):
    resp = client.post("/api/chat/session/batch", json={"action": "delete", "session_ids": [1]})
    assert resp.status_code in (400, 422)
    assert resp.json()["code"] == 40001
