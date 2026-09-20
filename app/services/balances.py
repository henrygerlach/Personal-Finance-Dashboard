"""
The Comdirect API session and the account balances it returns.

Credentials are a per-user login rather than process-wide configuration, so
the app never talks to the bank until a user asks it to, and no balance
answers for anyone but the user who logged in for it. There is no
process-wide cache: a snapshot is a Mongo read per page load, which is
cheaper than being wrong about whose balances are in memory.
"""

import asyncio
from datetime import datetime, timedelta

from app.config import get_logger
from app.crypto import decrypt, encrypt
from app.models import ComdirectBalanceSnapshot, ComdirectCredentials

logger = get_logger(__name__)

#: How long a started session waits for its photoTAN confirmation. Roughly
#: the bank's own challenge lifetime; past this the pending session is
#: dropped rather than held forever on a PIN nobody is going to confirm.
_PENDING_TIMEOUT = timedelta(minutes=5)


class ComdirectBalanceService:
    """Per-user Comdirect sessions and the balance snapshots they produce."""

    #: str(user_id) -> (ComdirectAuth, started_at). In-memory only: a
    #: pending session that outlives the process is one the bank has
    #: forgotten too, so nothing here needs to survive a restart.
    _pending: dict = {}

    @classmethod
    async def save_credentials(
        cls,
        user_id,
        client_id: str,
        client_secret: str,
        access_number: str,
        pin: str,
    ) -> None:
        """Encrypt and upsert one user's Comdirect login."""
        fields = {
            "client_id": encrypt(client_id),
            "client_secret": encrypt(client_secret),
            "access_number": encrypt(access_number),
            "pin": encrypt(pin),
        }
        existing = await ComdirectCredentials.find_one(
            ComdirectCredentials.user_id == user_id
        )
        if existing:
            for name, value in fields.items():
                setattr(existing, name, value)
            await existing.save()
        else:
            await ComdirectCredentials(user_id=user_id, **fields).insert()

    @classmethod
    async def status(cls, user_id) -> dict:
        """What the balances panel needs: state, and the latest stored data."""
        credentials = await ComdirectCredentials.find_one(
            ComdirectCredentials.user_id == user_id
        )
        latest = await (
            ComdirectBalanceSnapshot.find(ComdirectBalanceSnapshot.user_id == user_id)
            .sort("-loaded_at")
            .first_or_none()
        )
        pending_entry = cls._pending.get(str(user_id))
        pending = bool(
            pending_entry and datetime.utcnow() - pending_entry[1] < _PENDING_TIMEOUT
        )
        return {
            "has_credentials": credentials is not None,
            "pending": pending,
            "loaded_at": latest.loaded_at.isoformat() if latest else None,
            "balances": latest.balances if latest else None,
        }

    @classmethod
    async def begin_session(cls, user_id) -> dict:
        """Start a Comdirect login: this is what sends the photoTAN push."""
        credentials = await ComdirectCredentials.find_one(
            ComdirectCredentials.user_id == user_id
        )
        if credentials is None:
            raise ValueError("No Comdirect credentials saved for this user")

        from comdirect_api import ComdirectAuth

        auth = ComdirectAuth(
            client_id=decrypt(credentials.client_id),
            client_secret=decrypt(credentials.client_secret),
            access_number=decrypt(credentials.access_number),
            pin=decrypt(credentials.pin),
        )

        def start() -> None:
            auth.get_twofa_access_token()
            auth.create_session()
            auth.request_tan_challenge()

        try:
            await asyncio.to_thread(start)
        except Exception as error:
            cls._pending.pop(str(user_id), None)
            raise ValueError(f"Could not start the Comdirect session: {error}")

        cls._pending[str(user_id)] = (auth, datetime.utcnow())
        return {"pending": True}

    @classmethod
    async def confirm_session(cls, user_id) -> dict:
        """Finish a login after the photoTAN was confirmed in the bank's app."""
        entry = cls._pending.pop(str(user_id), None)
        if entry is None:
            raise ValueError("No pending Comdirect session for this user")
        auth, started_at = entry
        if datetime.utcnow() - started_at > _PENDING_TIMEOUT:
            raise ValueError("The photoTAN challenge has expired; start again")

        try:
            snapshot = await asyncio.to_thread(cls._finish_and_load, auth)
        except Exception as error:
            raise ValueError(f"Could not confirm the Comdirect session: {error}")

        await cls._store_snapshot(user_id, snapshot)
        return snapshot

    @classmethod
    def _finish_and_load(cls, auth) -> dict:
        """Blocking: confirm the TAN, get a token, and read every balance."""
        from comdirect_api import ComdirectClient

        auth.confirm_tan_challenge()
        auth.get_access_token()
        return cls._assemble_balances(ComdirectClient(auth))

    @classmethod
    def _assemble_balances(cls, client) -> dict:
        """
        Account, card and depot balances, flattened into one dict.

        The depot has no balance of its own — it is the sum of the positions
        in it, which is why it is added up here rather than read off a field.
        """
        accounts = client.get_account_balances()
        cards = client.get_card_balances()
        depots = client.get_depot_ids()

        balances = {}
        for account in accounts:
            balances[account.account.accountType.text] = account.balance.value
        for card in cards:
            balances[card.balance.card.cardType.text] = card.balance.balance.value

        positions = []
        for depot in depots:
            positions.extend(client.get_depot_positions(depot.depotId))

        depot_balance = 0.0
        for position in positions:
            depot_balance += float(position.currentValue.value)
        balances["Depot"] = depot_balance

        return {
            "loaded_at": datetime.utcnow().isoformat(),
            "balances": balances,
        }

    @classmethod
    async def _store_snapshot(cls, user_id, snapshot: dict) -> None:
        """Persist a loaded snapshot for this user."""
        loaded_at = datetime.fromisoformat(snapshot["loaded_at"])
        balances = {
            str(key): float(value or 0) for key, value in snapshot["balances"].items()
        }
        await ComdirectBalanceSnapshot(
            user_id=user_id, loaded_at=loaded_at, balances=balances
        ).insert()
