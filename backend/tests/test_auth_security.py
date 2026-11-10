"""认证安全工具离线测试：不连接数据库、不发起网络请求。"""
from __future__ import annotations

from services.security import (
    generate_token,
    hash_password,
    hash_token,
    verify_password,
)


def test_password_hash_roundtrip() -> None:
    stored = hash_password("Secret@123")
    assert stored.startswith("pbkdf2_sha256$")
    assert verify_password("Secret@123", stored) is True


def test_wrong_password_and_malformed_hash_rejected() -> None:
    stored = hash_password("Secret@123")
    assert verify_password("wrong-password", stored) is False
    assert verify_password("x", "malformed-hash") is False


def test_login_token_is_unique_and_hashable() -> None:
    token = generate_token()
    assert len(token) >= 24
    assert hash_token(token) == hash_token(token)
    assert hash_token(token) != hash_token(generate_token())
