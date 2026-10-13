"""密碼雜湊與驗證 —— 標準庫 PBKDF2-SHA256，無額外依賴。

格式：`pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>`
"""
import hashlib
import hmac
import os

_ALGO = "pbkdf2_sha256"
_ITER = 260_000
_SALT_BYTES = 16


def hash_password(password: str, *, iterations: int = _ITER) -> str:
    salt = os.urandom(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGO}${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = (stored or "").split("$")
        if algo != _ALGO:
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False
