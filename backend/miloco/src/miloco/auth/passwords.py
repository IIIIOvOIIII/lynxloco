from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError


class PasswordPolicyError(ValueError):
    pass


_PASSWORD_HASHER = PasswordHasher()


def validate_password_policy(password: str) -> None:
    if len(password) < 8:
        raise PasswordPolicyError("password_too_short")


def hash_password(password: str) -> str:
    validate_password_policy(password)
    return _PASSWORD_HASHER.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except (VerificationError, VerifyMismatchError):
        return False
