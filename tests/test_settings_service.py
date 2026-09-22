"""
Tests for SettingsService: admin derivation, and the two secrets it guards.

The admin is derived rather than stored — the oldest account by
``created_at`` — so there is nothing to migrate for the accounts that already
exist. The Anthropic key is app-wide, applied straight to the running
``settings`` object so a key saved through the page takes effect without a
restart. The Comdirect toggle is the one with an invariant to protect: it
must never be on without a login behind it, in either direction — enabling it
is refused without one, and revoking the login switches it back off.
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.services import SettingsService
from app.services import settings as settings_module
from tests.fixtures.odm import document, patch_services

USER_ID = "u" * 24
OTHER_USER_ID = "o" * 24


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(monkeypatch):
    models = SimpleNamespace(
        User=document("User"),
        ComdirectCredentials=document("ComdirectCredentials", "user_id"),
        AnthropicKey=document("AnthropicKey"),
    )
    patch_services(
        monkeypatch,
        User=models.User,
        ComdirectCredentials=models.ComdirectCredentials,
        AnthropicKey=models.AnthropicKey,
    )
    return models


def make_user(db, user_id, created_at, **extra):
    defaults = dict(id=user_id, created_at=created_at, balances_enabled=False)
    defaults.update(extra)
    user = db.User(**defaults)

    async def save():
        pass

    user.save = save
    db.User.rows.append(user)
    return user


# --- Admin derivation -----------------------------------------------------


def test_the_oldest_account_is_the_admin_and_a_later_one_is_not(db):
    first = make_user(db, USER_ID, datetime(2026, 1, 1))
    second = make_user(db, OTHER_USER_ID, datetime(2026, 2, 1))

    assert run(SettingsService.is_admin(first)) is True
    assert run(SettingsService.is_admin(second)) is False


# --- The Comdirect toggle ---------------------------------------------------


def test_enabling_balances_without_credentials_is_refused_and_does_not_save(db):
    user = make_user(db, USER_ID, datetime(2026, 1, 1))

    with pytest.raises(ValueError):
        run(SettingsService.set_balances_enabled(user, True))

    assert user.balances_enabled is False


def test_revoking_a_comdirect_login_also_switches_the_snapshot_off(db):
    user = make_user(db, USER_ID, datetime(2026, 1, 1), balances_enabled=True)
    credentials = db.ComdirectCredentials(user_id=USER_ID)
    credentials.delete = _deleter(db.ComdirectCredentials, credentials)
    db.ComdirectCredentials.rows.append(credentials)

    run(SettingsService.revoke_comdirect(user))

    assert user.balances_enabled is False
    assert credentials not in db.ComdirectCredentials.rows


def _deleter(model, row):
    async def delete():
        if row in model.rows:
            model.rows.remove(row)

    return delete


# --- The Anthropic key -------------------------------------------------------


def test_saving_the_key_applies_it_and_revoking_restores_the_env_value(db, monkeypatch):
    monkeypatch.setattr(settings_module, "_ENV_KEY", "from-dot-env")
    monkeypatch.setattr(settings, "anthropic_api_key", "from-dot-env")
    monkeypatch.setattr(
        settings, "comdirect_credentials_key", Fernet.generate_key().decode()
    )

    run(SettingsService.save_anthropic_key("sk-new-key"))

    assert settings.anthropic_api_key == "sk-new-key"
    assert len(db.AnthropicKey.rows) == 1
    assert run(SettingsService.anthropic_key()) == "sk-new-key"

    run(SettingsService.revoke_anthropic_key())

    assert settings.anthropic_api_key == "from-dot-env"
    assert db.AnthropicKey.rows == []
    assert run(SettingsService.anthropic_key()) is None
