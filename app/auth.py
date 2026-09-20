"""
Password hashing and the session-backed login every route depends on.
"""

import hashlib
import secrets

from fastapi import HTTPException, Request

from app.models import User, object_id

#: NIST's current floor for PBKDF2-HMAC-SHA256. Stdlib rather than a library:
#: bcrypt/argon2 would be the stronger choice for a public app, but this one
#: is shared with family and friends, not attackers with a stolen hash to
#: crack offline at scale.
_PBKDF2_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    """A password as ``salt:digest``, both hex, ready to store."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"{salt.hex()}:{digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    """Whether ``password`` is the one ``hash_password`` produced this from."""
    salt_hex, _, digest_hex = password_hash.partition(":")
    if not salt_hex or not digest_hex:
        return False
    salt = bytes.fromhex(salt_hex)
    expected = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt, _PBKDF2_ITERATIONS
    )
    return secrets.compare_digest(expected.hex(), digest_hex)


class AuthRequired(HTTPException):
    """No session, or a session naming a user that no longer exists."""

    def __init__(self) -> None:
        super().__init__(status_code=401, detail="Not authenticated")


async def current_user(request: Request) -> User:
    """
    The signed-in user, from the session cookie.

    A dependency rather than middleware so a route that needs the user's id
    to scope its own queries gets it for free — FastAPI resolves a dependency
    once per request no matter how many handlers ask for it.

    Raises:
        AuthRequired: Caught by the handler registered in app.main, which
            turns it into a redirect to /login for a page and a 401 for an
            /api/ call.
    """
    raw_id = request.session.get("user_id")
    if raw_id:
        try:
            user = await User.get(object_id(raw_id))
        except ValueError:
            user = None
        if user is not None:
            return user
    raise AuthRequired()
