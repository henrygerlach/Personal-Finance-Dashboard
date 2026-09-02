"""
Pieces every service page shares.

The drill-down list is the same list wherever it is opened from, so the
shape of one transaction and the summary of one side of a bar are defined
once here rather than per page.
"""


from app.config import get_logger
from app.normalization import merchant_of

logger = get_logger(__name__)

logger = get_logger(__name__)


#: How many transactions each direction of a drill-down returns. Enough to
#: show what actually drove the thing that was clicked, without turning the
#: popup into a statement listing.
TOP_DETAIL_TRANSACTIONS = 8


def _describe_transaction(transaction) -> dict:
    """
    One transaction as the dashboard's drill-down list needs it.

    The merchant is the recovered PayPal counterparty where there is one,
    because for those lines Comdirect's own booking text says only "PayPal".
    The raw booking text travels alongside it so the panel can show the line
    the bank actually recorded.
    """
    return {
        "id": str(transaction.id),
        "date": transaction.date.strftime("%Y-%m-%d"),
        "amount": transaction.amount,
        "merchant": merchant_of(transaction) or "Unnamed payment",
        "booking_text": transaction.booking_text,
        "major": transaction.major_category or "Other",
        "minor": transaction.minor_category or "Uncategorized",
        "is_recurring": bool(getattr(transaction, "is_recurring", False)),
    }


def _summarize_side(transactions: list, limit: int) -> dict:
    """
    One direction of a bar: its top transactions and what they leave out.

    ``rest`` is what the rows beyond the limit add up to, so the panel can say
    how much of the bar it is not showing instead of implying the list is the
    whole story.

    Args:
        transactions: Every transaction on this side, already ranked.
        limit: How many of them to return.
    """
    shown = transactions[:limit]
    total = sum(transaction.amount for transaction in transactions)
    shown_total = sum(transaction.amount for transaction in shown)
    return {
        "transactions": [_describe_transaction(t) for t in shown],
        "count": len(transactions),
        "hidden_count": len(transactions) - len(shown),
        "total": total,
        "rest": total - shown_total,
    }
