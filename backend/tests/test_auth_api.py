"""认证与鉴权依赖的 API 级离线回归（不连数据库、不调外部模型）。

覆盖：
1. 受保护接口在未登录时返回 40101（而不是参数校验 40001）——修复「request 被当作必填
   query 参数」后加的回归护栏；
2. 登录态有效时 /auth/me 返回本人信息；
3. 禁用账号持有效 Cookie 仍被拒绝；
4. member 调用管理员接口返回 40303；
5. OpenAPI 中受保护路由不得出现 request 查询参数。

所有仓储函数均由 monkeypatch 替换为内存假实现。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import main
from errors import BizError, ERR_SESSION_NOT_FOUND
from services import account_store

PROTECTED_ROUTES = [
    ("get", "/api/auth/me"),
    ("get", "/api/chat/session/list"),
    ("post", "/api/chat/session"),
    ("patch", "/api/chat/session/1"),
    ("delete", "/api/chat/session/1"),
    ("post", "/api/chat/stream"),
    ("get", "/api/chat/record/1"),
    ("post", "/api/docs/reindex"),
    ("get", "/api/docs/list"),
    ("get", "/api/system/info"),
    ("get", "/api/admin/users"),
    ("post", "/api/admin/users"),
    ("patch", "/api/admin/users/2/status"),
    ("post", "/api/admin/users/2/reset-password"),
]


def _user(role: str = "member", status: int = 1) -> dict:
    return {
        "user_id": 7,
        "username": "tester",
        "display_name": "测试用户",
        "role": role,
        "status": status,
    }


@pytest.fixture()
def client() -> TestClient:
    # 不进入 with 上下文：不触发 lifespan，因此不会连接数据库
    return TestClient(main.app)


def _patch_session(monkeypatch, session):
    async def fake_get_active_session(token_hash, renew_ttl_seconds=0):
        return session

    monkeypatch.setattr(account_store, "get_active_session", fake_get_active_session)


@pytest.mark.parametrize("method,path", PROTECTED_ROUTES)
def test_protected_routes_require_login(client: TestClient, method: str, path: str) -> None:
    resp = getattr(client, method)(path)
    assert resp.status_code == 401, f"{method.upper()} {path} -> {resp.status_code}"
    assert resp.json()["code"] == 40101
    # 回归护栏：不能退化成参数校验错误
    assert resp.json()["code"] != 40001


def test_me_returns_current_user(client: TestClient, monkeypatch) -> None:
    _patch_session(monkeypatch, _user())
    client.cookies.set("rag_session", "dummy-token")
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["username"] == "tester"
    assert data["role"] == "member"


def test_disabled_account_rejected_even_with_cookie(client: TestClient, monkeypatch) -> None:
    _patch_session(monkeypatch, _user(status=0))
    client.cookies.set("rag_session", "dummy-token")
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401
    assert resp.json()["code"] == 40101


def test_member_cannot_call_admin_api(client: TestClient, monkeypatch) -> None:
    _patch_session(monkeypatch, _user(role="member"))
    client.cookies.set("rag_session", "dummy-token")
    resp = client.post("/api/docs/reindex")
    assert resp.status_code == 403
    assert resp.json()["code"] == 40303


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/docs/list"),
        ("get", "/api/system/info"),
    ],
)
def test_member_cannot_read_admin_only_get_endpoints(
    client: TestClient, monkeypatch, method: str, path: str
) -> None:
    """文档列表与系统信息同属管理员视图，member 必须 403（不能只靠前端隐藏）。"""
    _patch_session(monkeypatch, _user(role="member"))
    client.cookies.set("rag_session", "dummy-token")
    resp = getattr(client, method)(path)
    assert resp.status_code == 403, f"{method.upper()} {path} -> {resp.status_code}"
    assert resp.json()["code"] == 40303


def test_admin_can_read_admin_only_get_endpoints(client: TestClient, monkeypatch) -> None:
    _patch_session(monkeypatch, _user(role="admin"))

    from services import vector_store

    async def fake_list_documents(page, size):
        return 1, [{"doc_id": 9, "doc_name": "samples/leave_policy.md", "chunk_count": 1}]

    monkeypatch.setattr(vector_store, "list_documents", fake_list_documents)
    client.cookies.set("rag_session", "dummy-token")

    resp = client.get("/api/docs/list")
    assert resp.status_code == 200
    assert resp.json()["data"]["list"][0]["doc_id"] == 9

    resp = client.get("/api/system/info")
    assert resp.status_code == 200
    assert resp.json()["data"]["embedding_dim"] > 0


def test_admin_passes_admin_guard(client: TestClient, monkeypatch) -> None:
    _patch_session(monkeypatch, _user(role="admin"))

    async def fake_reindex():
        return {"status": "reindexed"}

    from services import maintenance

    monkeypatch.setattr(maintenance, "rebuild_vector_index", fake_reindex)
    client.cookies.set("rag_session", "dummy-token")
    resp = client.post("/api/docs/reindex")
    assert resp.status_code == 200
    assert resp.json()["code"] == 0


def test_openapi_has_no_phantom_request_query_param() -> None:
    """任何 /api 路由都不得出现名为 request 的 query 参数（依赖注解缺失的特征）。"""
    spec = main.app.openapi()
    offenders = []
    for path, operations in spec.get("paths", {}).items():
        for method, operation in operations.items():
            for param in operation.get("parameters", []) or []:
                if param.get("name") == "request" and param.get("in") == "query":
                    offenders.append(f"{method.upper()} {path}")
    assert offenders == [], f"发现 request 查询参数：{offenders}"


def test_rename_session_passes_current_user_and_name(client: TestClient, monkeypatch) -> None:
    """路由必须把当前用户 ID 传给仓储层，不能只按 session_id 重命名。"""
    _patch_session(monkeypatch, _user())
    seen = {}

    async def fake_rename(session_id, user_id, session_name):
        seen.update(session_id=session_id, user_id=user_id, name=session_name)
        return {"session_id": session_id, "session_name": session_name, "create_time": None}

    from services import chat_store

    monkeypatch.setattr(chat_store, "rename_session", fake_rename)
    client.cookies.set("rag_session", "dummy-token")
    resp = client.patch("/api/chat/session/42", json={"session_name": "新名字"})
    assert resp.status_code == 200
    assert resp.json()["data"]["session_name"] == "新名字"
    assert seen == {"session_id": 42, "user_id": 7, "name": "新名字"}


def test_rename_other_users_session_returns_404(client: TestClient, monkeypatch) -> None:
    _patch_session(monkeypatch, _user())

    async def fake_rename(session_id, user_id, session_name):
        raise BizError(ERR_SESSION_NOT_FOUND, "会话不存在或无权访问", http_status=404)

    from services import chat_store

    monkeypatch.setattr(chat_store, "rename_session", fake_rename)
    client.cookies.set("rag_session", "dummy-token")
    resp = client.patch("/api/chat/session/99", json={"session_name": "x"})
    assert resp.status_code == 404
    assert resp.json()["code"] == 40402


def test_rename_rejects_blank_name(client: TestClient, monkeypatch) -> None:
    _patch_session(monkeypatch, _user())
    client.cookies.set("rag_session", "dummy-token")
    resp = client.patch("/api/chat/session/1", json={"session_name": ""})
    assert resp.status_code == 400
    assert resp.json()["code"] == 40001


# ---------------- 用户管理（P5） ----------------
def _patch_user_store(monkeypatch, target: dict, enabled_admins: int = 1, set_ok: bool = True):
    async def fake_get_user_by_id(user_id):
        return target if user_id == target["user_id"] else None

    async def fake_count_enabled_admins(include_user_id=None):
        return enabled_admins

    async def fake_set_user_status(user_id, status):
        return set_ok

    monkeypatch.setattr(account_store, "get_user_by_id", fake_get_user_by_id)
    monkeypatch.setattr(account_store, "count_enabled_admins", fake_count_enabled_admins)
    monkeypatch.setattr(account_store, "set_user_status", fake_set_user_status)


def test_cannot_disable_self(client: TestClient, monkeypatch) -> None:
    _patch_session(monkeypatch, _user(role="admin"))
    _patch_user_store(monkeypatch, {"user_id": 7, "username": "tester", "display_name": "测试用户",
                                    "role": "admin", "status": 1})
    client.cookies.set("rag_session", "dummy-token")
    resp = client.patch("/api/admin/users/7/status", json={"status": 0})
    assert resp.status_code == 400
    assert resp.json()["code"] == 40001
    assert "不能禁用自己" in resp.json()["msg"]


def test_cannot_disable_last_enabled_admin(client: TestClient, monkeypatch) -> None:
    _patch_session(monkeypatch, _user(role="admin"))
    _patch_user_store(monkeypatch, {"user_id": 8, "username": "other_admin", "display_name": "另一个管理员",
                                    "role": "admin", "status": 1}, enabled_admins=0)
    client.cookies.set("rag_session", "dummy-token")
    resp = client.patch("/api/admin/users/8/status", json={"status": 0})
    assert resp.status_code == 400
    assert "最后一个" in resp.json()["msg"]


def test_disable_member_ok(client: TestClient, monkeypatch) -> None:
    _patch_session(monkeypatch, _user(role="admin"))
    _patch_user_store(monkeypatch, {"user_id": 9, "username": "member1", "display_name": "普通用户",
                                    "role": "member", "status": 0})
    client.cookies.set("rag_session", "dummy-token")
    resp = client.patch("/api/admin/users/9/status", json={"status": 0})
    assert resp.status_code == 200
    assert resp.json()["code"] == 0


def test_disable_missing_user_returns_404(client: TestClient, monkeypatch) -> None:
    _patch_session(monkeypatch, _user(role="admin"))
    _patch_user_store(monkeypatch, {"user_id": 99, "username": "nobody", "display_name": "x",
                                    "role": "member", "status": 1})
    client.cookies.set("rag_session", "dummy-token")
    resp = client.patch("/api/admin/users/404/status", json={"status": 0})
    assert resp.status_code == 404
    assert resp.json()["code"] == 40403


def test_member_cannot_manage_users(client: TestClient, monkeypatch) -> None:
    _patch_session(monkeypatch, _user(role="member"))
    client.cookies.set("rag_session", "dummy-token")
    for method, path in (
        ("get", "/api/admin/users"),
        ("post", "/api/admin/users"),
        ("patch", "/api/admin/users/9/status"),
        ("post", "/api/admin/users/9/reset-password"),
    ):
        resp = getattr(client, method)(path)
        assert resp.status_code == 403, f"{method.upper()} {path} -> {resp.status_code}"
        assert resp.json()["code"] == 40303


# ---------------- 演示账号提示（默认关闭） ----------------
def test_demo_hint_disabled_when_switch_off(client: TestClient, monkeypatch) -> None:
    """开关关闭时必须完全不泄露；不依赖部署环境 .env 的实际取值。"""
    from config import settings

    monkeypatch.setattr(settings, "DEMO_CREDENTIAL_HINT", False)
    resp = client.get("/api/auth/demo-hint")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"enabled": False}


def _patch_demo_credentials(monkeypatch, password_hash: str, role: str = "admin", status: int = 1):
    from config import settings

    async def fake_user(username):
        return {"user_id": 1, "username": username, "display_name": "系统管理员",
                "role": role, "status": status}

    async def fake_cred(username):
        return {"user_id": 1, "password_hash": password_hash}

    monkeypatch.setattr(account_store, "get_user_by_username", fake_user)
    monkeypatch.setattr(account_store, "get_credentials_by_username", fake_cred)
    return settings


def test_demo_hint_shown_when_enabled_and_credentials_valid(client: TestClient, monkeypatch) -> None:
    from services import security

    settings = _patch_demo_credentials(
        monkeypatch, security.hash_password("Admin@12345")
    )
    monkeypatch.setattr(settings, "DEMO_CREDENTIAL_HINT", True)
    monkeypatch.setattr(settings, "DEFAULT_ADMIN_PASSWORD", "Admin@12345")
    monkeypatch.setattr(settings, "DEFAULT_ADMIN_USERNAME", "admin")

    resp = client.get("/api/auth/demo-hint")
    data = resp.json()["data"]
    assert data == {"enabled": True, "username": "admin", "password": "Admin@12345"}


def test_demo_hint_hidden_when_password_changed(client: TestClient, monkeypatch) -> None:
    from services import security

    settings = _patch_demo_credentials(
        monkeypatch, security.hash_password("another-password")
    )
    monkeypatch.setattr(settings, "DEMO_CREDENTIAL_HINT", True)
    monkeypatch.setattr(settings, "DEFAULT_ADMIN_PASSWORD", "Admin@12345")

    resp = client.get("/api/auth/demo-hint")
    assert resp.json()["data"] == {"enabled": False}


def test_demo_hint_hidden_for_disabled_account(client: TestClient, monkeypatch) -> None:
    from services import security

    settings = _patch_demo_credentials(
        monkeypatch, security.hash_password("Admin@12345"), status=0
    )
    monkeypatch.setattr(settings, "DEMO_CREDENTIAL_HINT", True)
    monkeypatch.setattr(settings, "DEFAULT_ADMIN_PASSWORD", "Admin@12345")

    resp = client.get("/api/auth/demo-hint")
    assert resp.json()["data"] == {"enabled": False}
