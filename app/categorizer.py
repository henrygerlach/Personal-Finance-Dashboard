"""
Transaction categorization logic based on rules.
"""
from typing import Sequence

from app.config import get_logger
from app.models import CategoryRule, Transaction

logger = get_logger(__name__)


class Categorizer:
    """Applies categorization rules to transactions."""

    def __init__(self, rules: Sequence[CategoryRule]) -> None:
        """
        Initialize categorizer with the provided rules in order.

        Args:
            rules: List of categorization rules
        """
        self.rules = list(rules)
        self._compiled_rules = [
            (
                str(rule.substring).upper(),
                str(rule.process or "").upper(),
                rule.major,
                rule.minor,
            )
            for rule in self.rules
        ]

    @staticmethod
    def _is_uncategorized(transaction: Transaction) -> bool:
        """Return whether a transaction is still in the default uncategorized state."""
        major = str(getattr(transaction, "major_category", None) or "Other").strip()
        minor = str(
            getattr(transaction, "minor_category", None) or "Uncategorized"
        ).strip()
        return major.lower() == "other" and minor.lower() == "uncategorized"

    def categorize(
        self, transactions: Sequence[Transaction], only_uncategorized: bool = False
    ) -> None:
        """
        Categorize a sequence of transactions in-place.

        Args:
            transactions: List of Transaction objects to categorize
            only_uncategorized: Only update transactions that are still in the
                default Other/Uncategorized state
        """
        for transaction in transactions:
            if only_uncategorized and not self._is_uncategorized(transaction):
                continue

            major, minor = self.apply_rules(transaction)
            transaction.major_category = major
            transaction.minor_category = minor

    def apply_rules(self, transaction: Transaction) -> tuple[str, str]:
        """
        Apply rules to a transaction and return the category.

        Checks each rule in the order provided by the rule list:
        1. If substring matches booking_text
        2. If process code matches (if specified)

        Returns the first matching rule's categories.

        Args:
            transaction: Transaction to categorize

        Returns:
            Tuple of (major_category, minor_category)
        """
        booking_text = transaction.booking_text.upper()
        process = transaction.process.upper() if transaction.process else ""

        for substring, required_process, major, minor in self._compiled_rules:
            # Check for substring match in booking text
            if substring not in booking_text:
                continue

            # If rule requires specific process, check it
            if required_process and required_process != process:
                continue

            # Rule matches (or no process requirement)
            return major, minor

        # No rule matched, return default uncategorized
        return "Other", "Uncategorized"

    async def recompute_categories(self) -> None:
        """
        Recompute categories for all transactions and save them.

        This is useful after adding a new rule to update all
        previously categorized transactions.
        """
        try:
            transactions = await Transaction.find_all().to_list()
            self.categorize(transactions=transactions)

            for transaction in transactions:
                await transaction.save()

            logger.info(f"Recomputed categories for {len(transactions)} transactions")
        except Exception as e:
            logger.error(f"Failed to recompute categories: {e}")
            raise
