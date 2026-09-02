"""
The queue of transactions a person still has to decide.
"""


from app.categories import MINOR_NEEDS_REVIEW, flow_type_for, is_valid_pair
from app.config import get_logger
from app.models import (
    SOURCE_USER,
    Transaction,
    object_id,
)
from app.normalization import normalize_merchant

logger = get_logger(__name__)

logger = get_logger(__name__)


class ReviewService:
    """The queue of transactions the classifier would not decide."""

    @staticmethod
    async def queue(limit: int = 100, skip: int = 0) -> list[Transaction]:
        """Transactions awaiting a human decision, least confident first."""
        return await (
            Transaction.find(Transaction.minor_category == MINOR_NEEDS_REVIEW)
            .sort("confidence", "-date")
            .skip(max(0, skip))
            .limit(limit)
            .to_list()
        )

    @staticmethod
    async def count() -> int:
        return await Transaction.find(
            Transaction.minor_category == MINOR_NEEDS_REVIEW
        ).count()

    @staticmethod
    async def paypal_context(transactions: list) -> dict:
        """
        What PayPal knew about each of these transactions, by transaction id.

        A PayPal booking line is the hardest thing in the queue to decide:
        Comdirect prints no payee on it, so once the payee has been recovered
        the rest of what PayPal recorded — who the two sides were, and
        whatever the payer wrote on the payment — is the only context a person
        has. A payment between two people frequently carries nothing else.

        Only transactions already matched to a settlement appear; the rest are
        ordinary lines with nothing to add.
        """
        from app.paypal import settlement_details

        by_code = {
            transaction.paypal_settlement_code: transaction
            for transaction in transactions
            if getattr(transaction, "paypal_settlement_code", None)
        }
        if not by_code:
            return {}

        found = await settlement_details(by_code)
        return {str(by_code[code].id): settlement for code, settlement in found.items()}

    @staticmethod
    async def correct(
        transaction_id: str, major: str, minor: str
    ) -> tuple[Transaction, bool]:
        """
        File a transaction by hand and teach the merchant cache.

        The correction is the point: it settles this transaction *and* every
        future one from the same merchant *running the same way*. The cache
        entry is written as ``user``, which the classifier is not allowed to
        overwrite.

        Returns:
            The updated transaction and whether the merchant cache was
            written — it is not, when the booking line holds no merchant to
            key on.
        """
        from app.classification.service import direction_of, remember_merchant

        transaction = await Transaction.get(object_id(transaction_id))
        if transaction is None:
            raise ValueError(f"No transaction {transaction_id}")

        # A hand correction is not coerced. Silently turning an invalid pair
        # into Needs Review would file the transaction as unreviewed *and*
        # teach the merchant cache that answer as if a person had chosen it —
        # a malformed form post would then poison every future transaction
        # from that merchant.
        if not is_valid_pair(major, minor):
            raise ValueError(f"{major!r} / {minor!r} is not a category")

        transaction.major_category = major
        transaction.minor_category = minor
        transaction.flow_type = flow_type_for(major)
        transaction.confidence = 1.0
        transaction.reason = "Categorised by hand."
        transaction.classification_source = SOURCE_USER
        await transaction.save()

        merchant = normalize_merchant(transaction.booking_text, transaction.process)
        if merchant:
            # Taught for this transaction's direction only. Correcting a
            # payment *to* someone must not overwrite what was learned from
            # the payments received *from* them, which mean something else.
            await remember_merchant(
                merchant,
                major,
                minor,
                SOURCE_USER,
                direction_of(transaction.amount),
            )
        return transaction, bool(merchant)
