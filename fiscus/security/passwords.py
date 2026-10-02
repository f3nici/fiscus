"""argon2id, the optional fallback. Off for everyone until an admin turns it on."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()  # argon2id with the library's current defaults
MIN_LENGTH = 12

# A dummy hash so a login for an unknown user costs the same as a real one.
_DUMMY = _hasher.hash('fiscus-timing-equaliser')


class PasswordPolicyError(ValueError):
    pass


def check_policy(password: str, username: str) -> None:
    if len(password) < MIN_LENGTH:
        raise PasswordPolicyError(f'Use at least {MIN_LENGTH} characters')
    if len(password) > 1024:
        raise PasswordPolicyError('That password is too long')
    if username and username.lower() in password.lower():
        raise PasswordPolicyError('The password cannot contain your username')


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify(stored: str | None, password: str) -> bool:
    try:
        return _hasher.verify(stored or _DUMMY, password) and stored is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
