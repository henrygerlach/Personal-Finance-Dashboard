"""
Tests for the Fernet encryption over stored Comdirect credentials.
"""

import pytest
from cryptography.fernet import Fernet

from app import crypto
from app.config import settings


def test_a_value_round_trips_through_encryption(monkeypatch):
    """The stored ciphertext must decrypt back to exactly what was put in."""
    monkeypatch.setattr(
        settings, "comdirect_credentials_key", Fernet.generate_key().decode()
    )

    ciphertext = crypto.encrypt("my-secret")

    assert ciphertext != "my-secret"
    assert crypto.decrypt(ciphertext) == "my-secret"


def test_encrypting_without_a_key_is_refused(monkeypatch):
    """A missing key must not silently store plaintext."""
    monkeypatch.setattr(settings, "comdirect_credentials_key", None)

    with pytest.raises(ValueError):
        crypto.encrypt("my-secret")
