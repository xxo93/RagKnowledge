"""确保存在一个可用的超级管理员账号（幂等，适合本地演示/重置环境）。

账号与密码来自 config.py：
    DEFAULT_ADMIN_USERNAME / DEFAULT_ADMIN_PASSWORD / DEFAULT_ADMIN_DISPLAY_NAME
可用 .env 或进程环境变量覆盖。

行为：
- 账号不存在：按默认配置创建（role=admin，拥有全部权限）；
- 账号已存在：默认不动密码；加 --reset-password 才把密码重置为配置值并吊销其全部登录态。

运行（cwd=backend）：
    python scripts/ensure_admin.py                    # 缺则创建，有不改
    python scripts/ensure_admin.py --reset-password   # 强制把密码重置为配置值
退出码：0=成功；1=数据库不可用或执行失败。

安全提示：默认密码仅为本地演示便利，正式使用请改密或把 DEFAULT_ADMIN_PASSWORD 置空
（置空后迁移脚本会随机生成密码并在控制台打印一次）。
"""
from __future__ import annotations

import argparse
import asyncio
import selectors
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from config import settings  # noqa: E402
from db import close_pool, init_pool  # noqa: E402
from services import account_store, security  # noqa: E402


async def main(reset_password: bool) -> int:
    username = (settings.DEFAULT_ADMIN_USERNAME or "admin").strip().lower()
    display = (settings.DEFAULT_ADMIN_DISPLAY_NAME or "系统管理员").strip()
    password = (settings.DEFAULT_ADMIN_PASSWORD or "").strip()

    if not await init_pool():
        print("ENSURE_ADMIN_RESULT=DB_UNAVAILABLE")
        return 1

    try:
        await account_store.ensure_auth_schema()
        user = await account_store.get_user_by_username(username)

        if user is None:
            if not password:
                print("ENSURE_ADMIN_RESULT=FAIL 未配置 DEFAULT_ADMIN_PASSWORD，无法创建账号")
                return 1
            user_id = await account_store.create_user(
                username, display, security.hash_password(password), "admin"
            )
            print(f"ENSURE_ADMIN_CREATED=YES user_id={user_id} username={username}")
        else:
            user_id = int(user["user_id"])
            if user.get("role") != "admin":
                print(
                    f"ENSURE_ADMIN_RESULT=FAIL username={username} 已存在但角色为 "
                    f"{user.get('role')}，请改用其他用户名或手工调整角色"
                )
                return 1
            if reset_password:
                if not password:
                    print("ENSURE_ADMIN_RESULT=FAIL 未配置 DEFAULT_ADMIN_PASSWORD，无法重置")
                    return 1
                await account_store.update_password(user_id, security.hash_password(password))
                revoked = await account_store.revoke_sessions_for_user(user_id)
                print(
                    f"ENSURE_ADMIN_PASSWORD_RESET=YES username={username} "
                    f"revoked_sessions={revoked}"
                )
            else:
                print(f"ENSURE_ADMIN_EXISTS=YES username={username}（密码未改动）")
                print("提示：如需重置为默认密码，追加 --reset-password")
    finally:
        await close_pool()

    print(f"DEFAULT_ADMIN_USERNAME={username}")
    print(f"DEFAULT_ADMIN_PASSWORD={password if password else '(随机生成，见迁移输出)'}")
    print("ENSURE_ADMIN_RESULT=OK")
    return 0


def _selector_factory() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop(selectors.DefaultSelector())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="确保存在可用的超级管理员账号")
    parser.add_argument(
        "--reset-password",
        action="store_true",
        help="把已存在的默认管理员密码重置为 DEFAULT_ADMIN_PASSWORD 并吊销其登录态",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.reset_password), loop_factory=_selector_factory))
