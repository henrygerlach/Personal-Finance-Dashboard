"""
The admin's Anthropic key and each user's Comdirect connection.

Two secrets live behind /settings, and they are owned differently. The
Anthropic key is app-wide and belongs to whichever account is derived as the
admin: the oldest ``User`` by ``created_at``, with no field and no migration
for the accounts that already exist. The Comdirect login is per user, and the
balance snapshot it powers is off until its owner turns it on and has
credentials to turn it on with — so ``set_balances_enabled`` refuses to
enable the flag without a saved login, and ``revoke_comdirect`` turns the
flag back off in the same call, so it can never be on without a login behind
it.
"""

from app.config import settings
from app.crypto import decrypt, encrypt
from app.models import AnthropicKey, ComdirectCredentials, User

#: The fallback to restore when the stored key is revoked, captured once at
#: import rather than read fresh each time — a running process's own .env
#: value, not whatever the environment happens to hold later.
_ENV_KEY = settings.anthropic_api_key


class SettingsService:
    """Admin derivation and the two secrets behind /settings."""

    @staticmethod
    async def admin_id():
        """The oldest account's id, or ``None`` before any account exists."""
        admin = await User.find().sort("+created_at").first_or_none()
        return admin.id if admin else None

    @classmethod
    async def is_admin(cls, user: User) -> bool:
        return user.id == await cls.admin_id()

    @staticmethod
    async def anthropic_state() -> dict:
        """
        What the settings page shows about the key, besides the key itself.

        ``stored`` tells a key saved in Mongo apart from one inherited from
        .env, since only the former can be revoked.
        """
        stored = await AnthropicKey.find_all().first_or_none()
        return {
            "configured": bool(settings.anthropic_api_key),
            "stored": stored is not None,
            "created_at": stored.created_at if stored else None,
        }

    @staticmethod
    async def anthropic_key() -> str | None:
        """The stored key, decrypted for the settings form. ``None`` if unset."""
        stored = await AnthropicKey.find_all().first_or_none()
        return decrypt(stored.key) if stored else None

    @staticmethod
    async def save_anthropic_key(raw: str) -> None:
        """Encrypt, upsert the single stored key, and apply it in-process."""
        ciphertext = encrypt(raw)
        existing = await AnthropicKey.find_all().first_or_none()
        if existing:
            existing.key = ciphertext
            await existing.save()
        else:
            await AnthropicKey(key=ciphertext).insert()
        settings.anthropic_api_key = raw

    @staticmethod
    async def revoke_anthropic_key() -> None:
        """Delete the stored key and fall back to whatever .env holds."""
        existing = await AnthropicKey.find_all().first_or_none()
        if existing:
            await existing.delete()
        settings.anthropic_api_key = _ENV_KEY

    @staticmethod
    async def load_anthropic_key() -> None:
        """Apply the stored key, if any, at startup."""
        existing = await AnthropicKey.find_all().first_or_none()
        if existing:
            settings.anthropic_api_key = decrypt(existing.key)

    @staticmethod
    async def set_balances_enabled(user: User, enabled: bool) -> None:
        """
        Flip the snapshot on or off.

        Raises:
            ValueError: Turning it on without a saved Comdirect login — the
                toggle must never be on with nothing behind it.
        """
        if enabled:
            credentials = await ComdirectCredentials.find_one(
                ComdirectCredentials.user_id == user.id
            )
            if credentials is None:
                raise ValueError("Save a Comdirect login before enabling balances.")
        user.balances_enabled = enabled
        await user.save()

    @staticmethod
    async def revoke_comdirect(user: User) -> None:
        """Delete this user's Comdirect login and switch the snapshot off."""
        credentials = await ComdirectCredentials.find_one(
            ComdirectCredentials.user_id == user.id
        )
        if credentials:
            await credentials.delete()
        user.balances_enabled = False
        await user.save()
