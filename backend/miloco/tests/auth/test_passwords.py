import pytest
from miloco.auth.passwords import (
    PasswordPolicyError,
    hash_password,
    validate_password_policy,
    verify_password,
)


def test_hash_password_uses_argon2_and_never_echoes_plaintext() -> None:
    password_hash = hash_password("correct horse battery")
    assert password_hash.startswith("$argon2")
    assert "correct horse battery" not in password_hash
    assert verify_password("correct horse battery", password_hash) is True
    assert verify_password("wrong horse battery", password_hash) is False
    assert verify_password("correct horse battery", "not-an-argon2-hash") is False


def test_password_policy_requires_eight_characters() -> None:
    with pytest.raises(PasswordPolicyError):
        validate_password_policy("short")
    validate_password_policy("12345678")
