from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_ph = PasswordHasher()
# Used so that unknown-user logins cost the same time as real ones.
_DUMMY_HASH = _ph.hash("dummy-password-for-constant-time")

MIN_LENGTH = 12


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        ok = _ph.verify(password_hash or _DUMMY_HASH, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return bool(ok and password_hash)


def needs_rehash(password_hash: str) -> bool:
    try:
        return _ph.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


_GEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"  # no 0/O, 1/l/I


def generate_password() -> str:
    """Strong, readable password, e.g. 'Kp7rT-mQ2xd-Ha9wE-c3VnZ' (~117 bits)."""
    import secrets

    return "-".join("".join(secrets.choice(_GEN_ALPHABET) for _ in range(5)) for _ in range(4))


def password_problem(password: str, email: str = "") -> str | None:
    if len(password) < MIN_LENGTH:
        return f"Password must be at least {MIN_LENGTH} characters."
    if len(password) > 256:
        return "Password is too long."
    if email and email.split("@")[0].lower() in password.lower() and len(email.split("@")[0]) >= 4:
        return "Password must not contain your email name."
    if len(set(password)) < 5:
        return "Password is too repetitive."
    return None
