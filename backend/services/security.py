"""认证安全工具：标准库 PBKDF2 密码哈希、不透明登录令牌生成/哈希。

不引入 bcrypt/passlib 等第三方依赖；密码不落明文，数据库只保存哈希格式串。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

_PBKDF2_ITERATIONS = 260_000
_ALGO = "pbkdf2_sha256"


def hash_password(password: str) -> str:
    """生成 `pbkdf2_sha256$iter$salt$hash` 格式密码哈希。"""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"{_ALGO}${_PBKDF2_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    """常量时间校验明文密码与存储哈希；格式损坏直接拒绝。"""
    try:
        algo, iter_text, salt_b64, hash_b64 = stored.split("$", 3)
        if algo != _ALGO:
            return False
        iterations = int(iter_text)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, base64.binascii.Error):
        return False


def generate_token() -> str:
    """生成不可预测的不透明登录令牌。"""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """数据库只保存令牌哈希，避免数据库文件泄露后令牌可直接重放。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
