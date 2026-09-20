"""
Fernet encryption for the Comdirect credentials stored per user.

The key never enters the database: it lives only in COMDIRECT_CREDENTIALS_KEY
in .env, so a database dump alone can never be turned back into a login.
"""

from cryptography.fernet import Fernet

from app.config import settings


def _fernet() -> Fernet:
    if not settings.comdirect_credentials_key:
        raise ValueError(
            "COMDIRECT_CREDENTIALS_KEY is not set; refusing to store credentials "
            "unencrypted"
        )
    return Fernet(settings.comdirect_credentials_key)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()
