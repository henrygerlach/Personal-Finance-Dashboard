"""
The Comdirect API session and the account balances it returns.
"""


import asyncio
from datetime import datetime

from app.config import get_logger
from app.models import (
    ComdirectBalanceSnapshot,
)

logger = get_logger(__name__)

logger = get_logger(__name__)


class ComdirectBalanceService:
    """Authenticate once and cache Comdirect balances in memory."""

    _snapshot: dict = {
        "status": "not-configured",
        "loaded_at": None,
        "error": None,
        "balances": None,
    }

    @classmethod
    async def initialize_startup_session(cls) -> None:
        """Authenticate once at startup and load account/card/depot balances."""
        from app.config import settings

        if not settings.comdirect_startup_auth_enabled:
            fallback_reason = "Comdirect startup auth is disabled by configuration"
            fallback_snapshot = await cls._load_latest_stored_snapshot(
                fallback_reason=fallback_reason
            )
            if fallback_snapshot:
                cls._snapshot = fallback_snapshot
                logger.info(
                    "Startup auth disabled; using latest stored Comdirect balance "
                    "snapshot"
                )
            else:
                cls._snapshot.update(
                    {
                        "status": "disabled-no-snapshot",
                        "loaded_at": None,
                        "error": (
                            "Startup auth disabled and no stored balance snapshot "
                            "is available"
                        ),
                        "balances": None,
                    }
                )
                logger.info(
                    "Comdirect startup auth disabled and no stored snapshot found"
                )
            return

        required = {
            "comdirect_client_id": settings.comdirect_client_id,
            "comdirect_client_secret": settings.comdirect_client_secret,
            "comdirect_access_number": settings.comdirect_access_number,
            "comdirect_pin": settings.comdirect_pin,
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            message = f"Missing Comdirect credentials: {', '.join(missing)}"
            fallback_snapshot = await cls._load_latest_stored_snapshot(
                fallback_reason=message
            )
            if fallback_snapshot:
                cls._snapshot = fallback_snapshot
            else:
                cls._snapshot.update({"status": "error", "error": message})
            logger.warning(message)
            return

        logger.info("Starting Comdirect startup authentication")
        try:
            snapshot = await asyncio.to_thread(cls._authenticate_and_load)
            await cls._store_snapshot(snapshot)
            cls._snapshot = snapshot
            logger.info("Comdirect balances loaded at startup")
        except Exception as e:
            error_message = f"Failed to authenticate/load Comdirect balances: {e}"
            fallback_snapshot = await cls._load_latest_stored_snapshot(
                fallback_reason=error_message
            )
            if fallback_snapshot:
                cls._snapshot = fallback_snapshot
                logger.warning(
                    "Using stored Comdirect balance snapshot due to authentication "
                    "failure"
                )
            else:
                cls._snapshot.update({"status": "error", "error": error_message})
            logger.error(error_message)

    @classmethod
    def _authenticate_and_load(cls) -> dict:
        """Blocking Comdirect auth + data retrieval executed in thread."""
        from comdirect_api import ComdirectAuth, ComdirectClient

        from app.config import settings

        auth = ComdirectAuth(
            client_id=settings.comdirect_client_id,
            client_secret=settings.comdirect_client_secret,
            access_number=settings.comdirect_access_number,
            pin=settings.comdirect_pin,
        )
        auth.authenticate()
        client = ComdirectClient(auth)

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
            "status": "ready",
            "loaded_at": datetime.utcnow().isoformat(),
            "error": None,
            "balances": balances,
        }

    @classmethod
    async def _store_snapshot(cls, snapshot: dict) -> None:
        """Persist a successful snapshot in MongoDB for fallback usage."""
        loaded_at_raw = snapshot.get("loaded_at")
        try:
            loaded_at = datetime.fromisoformat(loaded_at_raw)
        except Exception:
            loaded_at = datetime.utcnow()

        balances = snapshot.get("balances") or {}
        normalized_balances = {
            str(key): float(value or 0) for key, value in balances.items()
        }

        document = ComdirectBalanceSnapshot(
            loaded_at=loaded_at,
            balances=normalized_balances,
        )
        await document.insert()

    @classmethod
    async def _load_latest_stored_snapshot(cls, fallback_reason: str) -> dict | None:
        """Load latest persisted snapshot and mark it as stale fallback."""
        latest = await (
            ComdirectBalanceSnapshot.find_all().sort("-loaded_at").first_or_none()
        )
        if not latest:
            return None

        return {
            "status": "stale",
            "loaded_at": latest.loaded_at.isoformat(),
            "error": fallback_reason,
            "balances": latest.balances,
        }

    @classmethod
    def get_balances_snapshot(cls) -> dict:
        """Return current Comdirect balance snapshot."""
        return cls._snapshot

    @classmethod
    def should_render_balance_section(cls) -> bool:
        """Whether the dashboard should render the account balances section."""
        return cls._snapshot.get("status") != "disabled-no-snapshot"
