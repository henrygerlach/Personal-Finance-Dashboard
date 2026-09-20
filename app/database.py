"""
Database initialization and connection management.
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.config import get_logger, settings
from app.models import (
    CategoryRule,
    ComdirectBalanceSnapshot,
    ComdirectCredentials,
    DismissedTripProposal,
    MerchantCategory,
    PatternDismissal,
    PayPalRecord,
    RecurringOverride,
    Transaction,
    Trip,
    TripAssignment,
    User,
)

logger = get_logger(__name__)


class DatabaseManager:
    """Manages database connection and lifecycle."""

    def __init__(self) -> None:
        self.client: AsyncIOMotorClient | None = None
        self.database: AsyncIOMotorDatabase | None = None

    async def connect(self) -> None:
        """Initialize database connection."""
        logger.info(f"Connecting to MongoDB at {settings.mongodb_url}")
        self.client = AsyncIOMotorClient(settings.mongodb_url)
        self.database = self.client[settings.database_name]

        # Initialize Beanie with document models
        await init_beanie(
            database=self.database,
            document_models=[
                Transaction,
                CategoryRule,
                MerchantCategory,
                ComdirectBalanceSnapshot,
                ComdirectCredentials,
                PayPalRecord,
                RecurringOverride,
                Trip,
                TripAssignment,
                DismissedTripProposal,
                PatternDismissal,
                User,
            ],
        )
        logger.info("Database connection established and Beanie initialized")

    async def disconnect(self) -> None:
        """Close database connection."""
        if self.client:
            self.client.close()
            logger.info("Database connection closed")

    async def health_check(self) -> bool:
        """Check database connectivity."""
        try:
            if self.client:
                await self.client.admin.command("ping")
                return True
        except Exception as e:
            logger.error(f"Database health check failed: {e}")
        return False


db_manager = DatabaseManager()


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[DatabaseManager, None]:
    """Context manager for database operations."""
    await db_manager.connect()
    try:
        yield db_manager
    finally:
        await db_manager.disconnect()
