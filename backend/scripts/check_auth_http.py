"""认证链路集成校验（真实 PostgreSQL，不调用 LLM / Embedding）。

覆盖 P1 完成标准：
1. 登录（错误密码拒绝 / 正确密码通过 / 下发 HttpOnly Cookie）；
2. /api/auth/me 返回本人信息；
3. 本人改密：原密码错误拒绝、新旧相同拒绝、改密后旧密码失效、新密码可登录；
4. 改密吊销其他会话，保留当前会话；
5. 登出吊销当前令牌；
6. 禁用账号无法登录，且已签发的 Cookie 立即失效；
7. 会话过期被拒绝；滑动续期生效（剩余不足一半时续满 7 天）。

运行（cwd=backend）：
    python scripts/check_auth_http.py
退出码：0=ALL_PASS；1=HAS_FAIL / DB_UNAVAILABLE。
临时账号在 finally 中清理，不污染演示基线。
"""
from __future__ import annotations

import asyncio
import selectors
import sys
import uuid
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

import httpx  # noqa: E402

from db import close_pool, init_pool  # noqa: E402
from services import account_store, security  # noqa: E402

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((f"{name}{(' | ' + detail) if detail else ''}", bool(ok)))


async def _set_session_expiry(token: str, sql_interval: str) -> None:
    pool = account_store.get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE auth_session SET expires_at = now() + %s::interval WHERE token_hash = %s",
                    (sql_interval, security.hash_token(token)),
                )


async def _session_remaining_hours(token: str) -> float:
    """剩余有效小时数；expires_at 为 timestamp without tz，统一用 now()::timestamp 比较。"""
    pool = account_store.get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT extract(epoch FROM (expires_at - now()::timestamp))::float "
                "FROM auth_session WHERE token_hash = %s",
                (security.hash_token(token),),
            )
            row = await cur.fetchone()
            return float(row[0]) / 3600 if row else -1.0


async def _set_status(user_id: int, status: int) -> None:
    pool = account_store.get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE app_user SET status = %s WHERE user_id = %s", (status, user_id)
                )


async def _max_log_id() -> int:
    pool = account_store.get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT coalesce(max(log_id), 0) FROM operation_log")
            return int((await cur.fetchone())[0])


async def _audit_rows_since(log_id: int) -> int:
    pool = account_store.get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM operation_log WHERE log_id > %s", (int(log_id),)
            )
            return int((await cur.fetchone())[0])


async def _cleanup_audit_since(log_id: int) -> None:
    pool = account_store.get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM operation_log WHERE log_id > %s", (int(log_id),)
                )


async def main() -> int:
    if not await init_pool():
        print("AUTH_HTTP_RESULT=DB_UNAVAILABLE")
        return 1

    username = f"qa_auth_{uuid.uuid4().hex[:8]}"
    old_pwd = "QaOld@12345"
    new_pwd = "QaNew@67890"
    user_id = await account_store.create_user(
        username, "QA Auth", security.hash_password(old_pwd), "member"
    )
    print(f"TEMP_USER={username} id={user_id}")
    managed_id = None

    admin_name = f"qa_admin_{uuid.uuid4().hex[:8]}"
    admin_pwd = "QaAdmin@12345"
    baseline_log_id = await _max_log_id()
    admin_id = await account_store.create_user(
        admin_name, "QA Admin", security.hash_password(admin_pwd), "admin"
    )
    print(f"TEMP_ADMIN={admin_name} id={admin_id}")

    transport = httpx.ASGITransport(app=_app())
    try:
        # ---------------- 1. 登录 ----------------
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/api/auth/login", json={"username": username, "password": "wrong-pwd"})
            body = r.json()
            check("错误密码登录被拒绝", r.status_code == 401 and body.get("code") == 40101,
                  f"HTTP={r.status_code} code={body.get('code')}")

            r = await c.post("/api/auth/login", json={"username": username, "password": old_pwd})
            body = r.json()
            check("正确密码登录成功",
                  r.status_code == 200 and body.get("code") == 0 and body["data"]["role"] == "member",
                  f"HTTP={r.status_code} role={body.get('data', {}).get('role')}")
            token = c.cookies.get("rag_session")
            check("下发 rag_session 会话令牌", bool(token), f"len={len(token or '')}")

            r = await c.get("/api/auth/me")
            body = r.json()
            check("登录后 /auth/me 正常",
                  r.status_code == 200 and body["data"]["username"] == username,
                  f"HTTP={r.status_code} username={body.get('data', {}).get('username')}")

            r = await c.get("/api/chat/session/list")
            check("登录后可访问受保护接口", r.status_code == 200, f"HTTP={r.status_code}")

            # ---------------- 2. 改密 ----------------
            r = await c.post("/api/auth/password",
                             json={"old_password": "bad-old", "new_password": new_pwd})
            check("改密：原密码错误被拒绝", r.status_code == 400 and r.json().get("code") == 40101,
                  f"HTTP={r.status_code} code={r.json().get('code')}")

            r = await c.post("/api/auth/password",
                             json={"old_password": old_pwd, "new_password": old_pwd})
            check("改密：新旧密码相同被拒绝", r.status_code == 400 and r.json().get("code") == 40001,
                  f"HTTP={r.status_code} code={r.json().get('code')}")

            # 第二个会话（改密后应被吊销）
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as other:
                r = await other.post("/api/auth/login", json={"username": username, "password": old_pwd})
                check("第二会话登录成功", r.status_code == 200, f"HTTP={r.status_code}")
                other_token = other.cookies.get("rag_session")

                r = await c.post("/api/auth/password",
                                 json={"old_password": old_pwd, "new_password": new_pwd})
                body = r.json()
                check("改密成功并返回吊销数量",
                      r.status_code == 200 and body["data"]["revoked_sessions"] >= 1,
                      f"HTTP={r.status_code} revoked={body.get('data', {}).get('revoked_sessions')}")

                r = await c.get("/api/auth/me")
                check("改密后当前会话仍有效", r.status_code == 200, f"HTTP={r.status_code}")

                r = await other.get("/api/auth/me")
                check("改密后其他会话被吊销", r.status_code == 401, f"HTTP={r.status_code}")
                check("其他会话令牌确实不同", other_token != token, "distinct tokens")

                # 旧密码不可再登录，新密码可登录
                r = await other.post("/api/auth/login",
                                     json={"username": username, "password": old_pwd})
                check("旧密码不可再登录", r.status_code == 401, f"HTTP={r.status_code}")
                r = await other.post("/api/auth/login",
                                     json={"username": username, "password": new_pwd})
                check("新密码可登录", r.status_code == 200, f"HTTP={r.status_code}")

            # ---------------- 3. 权限矩阵：管理员专属只读端点 ----------------
            r = await c.get("/api/docs/list")
            check("member 读文档列表被拒绝", r.status_code == 403 and r.json().get("code") == 40303,
                  f"HTTP={r.status_code} code={r.json().get('code')}")
            r = await c.get("/api/system/info")
            check("member 读系统信息被拒绝", r.status_code == 403 and r.json().get("code") == 40303,
                  f"HTTP={r.status_code} code={r.json().get('code')}")

            async with httpx.AsyncClient(transport=transport, base_url="http://test") as anon:
                r = await anon.get("/api/docs/list")
                check("匿名读文档列表被拒绝", r.status_code == 401, f"HTTP={r.status_code}")
                r = await anon.get("/api/system/info")
                check("匿名读系统信息被拒绝", r.status_code == 401, f"HTTP={r.status_code}")

            async with httpx.AsyncClient(transport=transport, base_url="http://test") as adm:
                r = await adm.post("/api/auth/login",
                                   json={"username": admin_name, "password": admin_pwd})
                check("管理员登录成功", r.status_code == 200, f"HTTP={r.status_code}")
                r = await adm.get("/api/docs/list")
                check("admin 读文档列表成功", r.status_code == 200, f"HTTP={r.status_code}")
                r = await adm.get("/api/system/info")
                check("admin 读系统信息成功", r.status_code == 200, f"HTTP={r.status_code}")

                # ---------------- 4. 用户管理（P5） ----------------
                managed_name = f"qa_managed_{uuid.uuid4().hex[:8]}"
                managed_pwd = "QaManaged@123"
                r = await adm.post("/api/admin/users", json={
                    "username": managed_name,
                    "display_name": "QA Managed",
                    "password": managed_pwd,
                    "role": "member",
                })
                created = r.json().get("data") or {}
                managed_id = created.get("user_id")
                check("admin 创建 member 账号",
                      r.status_code == 200 and managed_id,
                      f"HTTP={r.status_code} user_id={managed_id}")

                r = await adm.post("/api/admin/users", json={
                    "username": managed_name, "password": managed_pwd, "role": "member",
                })
                check("重复用户名被拒绝",
                      r.status_code == 400 and r.json().get("code") == 40004,
                      f"HTTP={r.status_code} code={r.json().get('code')}")

                r = await adm.get("/api/admin/users")
                payload = r.json().get("data") or {}
                names = [u["username"] for u in payload.get("list", [])]
                check("用户列表包含新账号",
                      r.status_code == 200 and managed_name in names,
                      f"HTTP={r.status_code} total={len(names)}")
                check("用户列表返回分页字段",
                      payload.get("page") == 1 and payload.get("size") == 20,
                      f"page={payload.get('page')} size={payload.get('size')}")
                check("用户列表不返回密码哈希",
                      all("password_hash" not in u for u in payload.get("list", [])),
                      "no password_hash")

                async with httpx.AsyncClient(transport=transport, base_url="http://test") as mu:
                    r = await mu.post("/api/auth/login",
                                      json={"username": managed_name, "password": managed_pwd})
                    check("新账号可登录", r.status_code == 200, f"HTTP={r.status_code}")

                    new_managed_pwd = "QaReset@456"
                    r = await adm.post(f"/api/admin/users/{managed_id}/reset-password",
                                       json={"new_password": new_managed_pwd})
                    check("admin 重置他人密码并吊销其会话",
                          r.status_code == 200 and r.json()["data"]["revoked_sessions"] >= 1,
                          f"HTTP={r.status_code} revoked={r.json().get('data', {}).get('revoked_sessions')}")

                    r = await mu.get("/api/auth/me")
                    check("被重置密码的账号旧会话失效", r.status_code == 401, f"HTTP={r.status_code}")
                    r = await mu.post("/api/auth/login",
                                      json={"username": managed_name, "password": managed_pwd})
                    check("被重置后旧密码不可登录", r.status_code == 401, f"HTTP={r.status_code}")
                    r = await mu.post("/api/auth/login",
                                      json={"username": managed_name, "password": new_managed_pwd})
                    check("被重置后新密码可登录", r.status_code == 200, f"HTTP={r.status_code}")

                    r = await adm.patch(f"/api/admin/users/{managed_id}/status", json={"status": 0})
                    check("admin 禁用账号", r.status_code == 200, f"HTTP={r.status_code}")
                    r = await mu.get("/api/auth/me")
                    check("禁用后其 Cookie 立即失效", r.status_code == 401, f"HTTP={r.status_code}")
                    r = await mu.post("/api/auth/login",
                                      json={"username": managed_name, "password": new_managed_pwd})
                    check("禁用后无法登录", r.status_code == 401, f"HTTP={r.status_code}")

                    r = await adm.patch(f"/api/admin/users/{managed_id}/status", json={"status": 1})
                    check("admin 启用账号", r.status_code == 200, f"HTTP={r.status_code}")
                    r = await mu.post("/api/auth/login",
                                      json={"username": managed_name, "password": new_managed_pwd})
                    check("启用后可登录", r.status_code == 200, f"HTTP={r.status_code}")

                r = await adm.patch(f"/api/admin/users/{admin_id}/status", json={"status": 0})
                check("禁止禁用自己",
                      r.status_code == 400 and r.json().get("code") == 40001,
                      f"HTTP={r.status_code} code={r.json().get('code')}")

            audit_rows = await _audit_rows_since(baseline_log_id)
            check("审计记录已写入（登录/用户管理/文档操作）",
                  audit_rows >= 5, f"rows={audit_rows}")

            # ---------------- 5. 会话过期与滑动续期 ----------------
            token = c.cookies.get("rag_session")
            await _set_session_expiry(token, "-1 second")
            r = await c.get("/api/auth/me")
            check("过期会话被拒绝", r.status_code == 401, f"HTTP={r.status_code}")

            await _set_session_expiry(token, "1 hour")
            r = await c.get("/api/auth/me")
            remaining_hours = await _session_remaining_hours(token)
            check("滑动续期把剩余有效期续满 7 天",
                  r.status_code == 200 and remaining_hours > 24 * 6,
                  f"HTTP={r.status_code} remaining={remaining_hours:.1f}h")

            # ---------------- 6. 登出 ----------------
            r = await c.post("/api/auth/logout")
            check("登出成功", r.status_code == 200, f"HTTP={r.status_code}")
            r = await c.get("/api/auth/me")
            check("登出后旧令牌失效", r.status_code == 401, f"HTTP={r.status_code}")

        # ---------------- 7. 禁用账号 ----------------
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as d:
            r = await d.post("/api/auth/login", json={"username": username, "password": new_pwd})
            check("禁用前可登录", r.status_code == 200, f"HTTP={r.status_code}")
            await _set_status(user_id, 0)
            r = await d.get("/api/auth/me")
            check("禁用后已签发 Cookie 立即失效", r.status_code == 401, f"HTTP={r.status_code}")
            r = await d.post("/api/auth/login", json={"username": username, "password": new_pwd})
            check("禁用账号无法登录", r.status_code == 401, f"HTTP={r.status_code}")
    finally:
        for temp_id in (user_id, admin_id, managed_id):
            if temp_id is None:
                continue
            try:
                await account_store.delete_user(temp_id)
                print(f"CLEANED_USER={temp_id}")
            except Exception as exc:  # noqa: BLE001
                print(f"CLEANUP_FAILED={temp_id}: {exc}")
        try:
            await _cleanup_audit_since(baseline_log_id)
            print("CLEANED_AUDIT_ROWS=YES")
        except Exception as exc:  # noqa: BLE001
            print(f"CLEANUP_AUDIT_FAILED={exc}")
        await close_pool()

    for name, ok in results:
        print(("PASS" if ok else "FAIL"), "-", name)
    passed = all(ok for _, ok in results)
    print("AUTH_HTTP_RESULT =", "ALL_PASS" if passed else "HAS_FAIL")
    return 0 if passed else 1


def _app():
    from main import app  # 延迟导入：确保 sys.path 与配置已就绪

    return app


def _selector_factory() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop(selectors.DefaultSelector())


if __name__ == "__main__":
    sys.exit(asyncio.run(main(), loop_factory=_selector_factory))
