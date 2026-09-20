"""
Recomputing categories, and how many are still waiting for one.
"""


from typing import Optional

from beanie import PydanticObjectId

from app.config import get_logger
from app.models import (
    Transaction,
)

logger = get_logger(__name__)


class CategorizationService:
    """Service for transaction categorization operations."""

    @staticmethod
    async def get_uncategorized_transaction(
        user_id: PydanticObjectId, skip: int = 0
    ) -> Optional[Transaction]:
        """
        Get an uncategorized transaction, most recent first.

        Args:
            user_id: Whose ledger to search.
            skip: How many transactions to step past, so the review UI can move
                to the next item without categorizing the current one.
        """
        results = await (
            Transaction.find(
                Transaction.user_id == user_id,
                Transaction.minor_category == "Uncategorized",
            )
            .sort("-date")
            .skip(max(0, skip))
            .limit(1)
            .to_list()
        )
        return results[0] if results else None

    @staticmethod
    async def get_uncategorized_count(user_id: PydanticObjectId) -> int:
        """Get the count of uncategorized transactions."""
        return await Transaction.find(
            Transaction.user_id == user_id,
            Transaction.minor_category == "Uncategorized",
        ).count()

    @staticmethod
    async def recompute_all_categories(
        user_id: PydanticObjectId, categorizer, only_uncategorized: bool = False
    ) -> None:
        """
        Recompute categories for transactions.

        Args:
            user_id: Whose ledger to recompute.
            categorizer: Categorizer instance with rules
            only_uncategorized: Only re-evaluate transactions still marked as
                Other/Uncategorized
        """
        try:
            transactions = await Transaction.find(
                Transaction.user_id == user_id
            ).to_list()
            transactions_to_process = [
                transaction
                for transaction in transactions
                if not only_uncategorized or categorizer._is_uncategorized(transaction)
            ]
            categorizer.categorize(
                transactions=transactions_to_process,
                only_uncategorized=False,
            )

            for transaction in transactions_to_process:
                await transaction.save()
            logger.info(
                "Recomputed categories for %s transactions",
                len(transactions_to_process),
            )
        except Exception as e:
            logger.error(f"Failed to recompute categories: {e}")
            raise
