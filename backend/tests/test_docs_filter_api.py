"""文档列表筛选 API 离线回归：日期参数解析、参数校验、权限边界。"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend.services import account_store, vector_store as vs


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
    return TestClient(main.app)


def _login_as(monkeypatch, role: str = "admin") -> TestClient:
    user = _user(role)

    async def fake_get_active_session(token_hash, renew_ttl_seconds=0):
        return user

    monkeypatch.setattr(account_store, "get_active_session", fake_get_active_session)
    return user


def test_list_dates_are_passed_to_store(client: TestClient, monkeypatch):
    """start_date/end_date 解析为 date 后传给仓储；结束日语义由 SQL 层保证。"""
    _login_as(monkeypatch)
    captured: dict = {}

    async def fake_list(page, size, keyword=None, status=None, start_date=None, end_date=None):
        captured.update({
            "page": page,
            "size": size,
            "keyword": keyword,
            "status": status,
            "start_date": start_date,
            "end_date": end_date,
        })
        return 0, []

    monkeypatch.setattr(vs, "list_documents", fake_list)
    client.cookies.set("rag_session", "test-token")
    resp = client.get("/api/docs/list?start_date=2026-11-01&end_date=2026-11-20&keyword=图片&status=1")

    assert resp.status_code == 200
    assert captured["start_date"] == date(2026, 11, 1)
    assert captured["end_date"] == date(2026, 11, 20)
    assert captured["keyword"] == "图片"
    assert captured["status"] == 1


def test_list_invalid_date_returns_40001(client: TestClient, monkeypatch):
    _login_as(monkeypatch)
    client.cookies.set("rag_session", "test-token")
    resp = client.get("/api/docs/list?start_date=2026-13-01")
    assert resp.status_code == 400
    assert resp.json()["code"] == 40001
    assert "YYYY-MM-DD" in resp.json()["msg"]


def test_list_start_after_end_returns_40001(client: TestClient, monkeypatch):
    _login_as(monkeypatch)
    client.cookies.set("rag_session", "test-token")
    resp = client.get("/api/docs/list?start_date=2026-11-20&end_date=2026-11-01")
    assert resp.status_code == 400
    assert resp.json()["code"] == 40001
    assert "开始日期不能晚于结束日期" in resp.json()["msg"]


def test_list_member_forbidden(client: TestClient, monkeypatch):
    _login_as(monkeypatch, role="member")
    client.cookies.set("rag_session", "test-token")
    resp = client.get("/api/docs/list")
    assert resp.status_code == 403
    assert resp.json()["code"] == 40303
