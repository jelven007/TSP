"""Pure password primitives compatible with legacy PBKDF2 records."""

import hashlib
import re
import secrets

ITERATIONS = 200_000
DUMMY_SALT = "00" * 16
DUMMY_HASH = hashlib.pbkdf2_hmac(
    "sha256", b"invalid-account", bytes.fromhex(DUMMY_SALT), ITERATIONS
).hex()


def normalize_email(value: str) -> str:
    value = value.strip().casefold()
    if len(value) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        raise ValueError("invalid_email")
    if value.startswith(".") or ".." in value:
        raise ValueError("invalid_email")
    return value


def hash_password(password: str) -> tuple[str, str]:
    if not 8 <= len(password) <= 128:
        raise ValueError("password_length")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), ITERATIONS
    ).hex()
    return salt, digest


def valid_record(salt: str, digest: str) -> bool:
    try:
        return len(bytes.fromhex(salt)) == 16 and len(bytes.fromhex(digest)) == 32
    except (TypeError, ValueError):
        return False


def verify_password(password: str, salt: str, digest: str) -> bool:
    valid = valid_record(salt, digest)
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt if valid else DUMMY_SALT), ITERATIONS
    )
    return secrets.compare_digest(actual, bytes.fromhex(digest if valid else DUMMY_HASH)) and valid


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
