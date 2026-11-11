"""多用户会话隔离数据库检查（不调用 LLM / Embedding）。

验证：
1. admin 会话列表不包含 member 临时会话，反之亦然；
2. member 不能删除 admin 会话（返回 40402）；
3. 越权删除不得销毁他人聊天记录（先按 owner 锁定再删记录）；
4. member 不能重命名 admin 会话，且他人会话名保持不变；
5. member 可重命名自己的会话；
6. 测试账号与临时会话在 finally 中自动清理，不污染演示基线。

运行（cwd=backend）：
    python scripts/check_auth_isolation.py
退出码：0=ALL_PASS；1=HAS_FAIL。
"""
from __future__ import annotations

import asyncio
import selectors
import secrets
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from db import close_pool, init_pool  # noqa: E402
from errors import BizError  # noqa: E402
from services import account_store, chat_store  # noqa: E402

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((f"{name}{(' | ' + detail) if detail else ''}", bool(ok)))


async def main() -> int:
    if not await init_pool():
        print("AUTH_ISOLATION_RESULT=DB_UNAVAILABLE")
        return 1

    admin_sid: int | None = None
    member_sid: int | None = None
    member_id: int | None = None
    admin_id: int | None = None

    try:
        admin_id = await account_store.get_first_admin_id()
        if admin_id is None:
            check("初始 admin 存在", False, "未找到 admin")
            return 1

        admin_sid = (await chat_store.create_session(admin_id, "QA-admin-isolation"))["session_id"]
        member_name = f"qa_member_{secrets.token_hex(4)}"
        member_id = await account_store.create_user(
            member_name, "QA Member", "unused-test-hash", "member"
        )
        member_sid = (await chat_store.create_session(member_id, "QA-member-isolation"))["session_id"]
        # 给 admin 会话落一条记录，用于验证越权删除不会清空他人记录
        await chat_store.insert_record(admin_sid, "admin-question", "admin-answer")

        # ---------------- 列表隔离 ----------------
        _, admin_list = await chat_store.list_sessions(admin_id)
        _, member_list = await chat_store.list_sessions(member_id)

        check("admin 列表包含 admin 会话",
              any(s["session_id"] == admin_sid for s in admin_list))
        check("admin 列表不包含 member 会话",
              not any(s["session_id"] == member_sid for s in admin_list))
        check("member 列表包含 member 会话",
              any(s["session_id"] == member_sid for s in member_list))
        check("member 列表不包含 admin 会话",
              not any(s["session_id"] == admin_sid for s in member_list))

        # ---------------- 删除越权 ----------------
        rejected = False
        try:
            await chat_store.delete_session(admin_sid, member_id)
        except BizError as exc:
            rejected = exc.http_status == 404
        check("member 删除 admin 会话被拒绝", rejected)
        admin_records = await chat_store.list_records(admin_sid)
        check("越权删除未销毁 admin 聊天记录",
              len(admin_records[1]) == 1,
              f"records={len(admin_records[1])}")
        check("越权删除后 admin 会话仍存在",
              await chat_store.get_session_for_user(admin_sid, admin_id) is not None)

        # ---------------- 重命名越权 ----------------
        rejected = False
        try:
            await chat_store.rename_session(admin_sid, member_id, "hacked-by-member")
        except BizError as exc:
            rejected = exc.http_status == 404
        check("member 重命名 admin 会话被拒绝", rejected)

        admin_session = await chat_store.get_session_for_user(admin_sid, admin_id)
        check("越权重命名未改变 admin 会话名",
              admin_session is not None and admin_session["session_name"] == "QA-admin-isolation",
              f"name={admin_session['session_name'] if admin_session else None}")

        renamed = await chat_store.rename_session(member_sid, member_id, "member-renamed")
        check("member 可重命名自己的会话",
              renamed["session_name"] == "member-renamed", f"name={renamed['session_name']}")

        empty_rejected = False
        try:
            await chat_store.rename_session(member_sid, member_id, "   ")
        except BizError as exc:
            empty_rejected = exc.code == 40001
        check("空会话名被拒绝", empty_rejected)

        long_name = "N" * 200
        truncated = await chat_store.rename_session(member_sid, member_id, long_name)
        check("超长会话名截断到 128", len(truncated["session_name"]) == 128,
              f"len={len(truncated['session_name'])}")
    finally:
        # 先清理临时会话，再删除测试账号；任何清理异常不掩盖主检查结果
        for sid in (member_sid, admin_sid):
            if sid is not None and admin_id is not None:
                try:
                    await chat_store.delete_session(sid, admin_id)
                except Exception:  # noqa: BLE001
                    pass
        if member_id is not None:
            try:
                await account_store.delete_user(member_id)
            except Exception:  # noqa: BLE001
                pass
        await close_pool()

    for name, ok in results:
        print(("PASS" if ok else "FAIL"), "-", name)
    passed = all(ok for _, ok in results)
    print("AUTH_ISOLATION_RESULT =", "ALL_PASS" if passed else "HAS_FAIL")
    return 0 if passed else 1


def _selector_factory() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop(selectors.DefaultSelector())


if __name__ == "__main__":
    sys.exit(asyncio.run(main(), loop_factory=_selector_factory))
