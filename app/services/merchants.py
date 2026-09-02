"""
Who actually gets the money, and how few of them there are.
"""


from app.config import get_logger
from app.merchants import (
    LEADERBOARD,
    MIN_CATEGORY_MERCHANTS,
    WINDOW_MONTHS,
)
from app.merchants import analyse as concentration
from app.models import (
    Transaction,
)
from app.services.common import _describe_transaction

logger = get_logger(__name__)

logger = get_logger(__name__)


class MerchantService:
    """Where the money goes, below the category level."""

    @staticmethod
    def _render(merchant, by_id: dict) -> dict:
        """
        One merchant as the page draws it, its largest payments included.

        The transactions travel with the row rather than behind a second
        request, the way the patterns page ships the proof of a finding: the
        row makes a claim about who was paid, and a list that arrives later is
        a list that can be read after the claim it was meant to support.
        """
        rows = [by_id[tid] for tid in merchant.transaction_ids if tid in by_id]
        return {
            "key": merchant.key,
            "name": merchant.name,
            "category": merchant.category,
            "major": merchant.major,
            "total": merchant.total,
            "share": merchant.share,
            "visits": merchant.visits,
            "ticket": merchant.ticket,
            "first_seen": merchant.first_seen,
            "last_seen": merchant.last_seen,
            "previous_total": merchant.previous_total,
            "previous_visits": merchant.previous_visits,
            "previous_ticket": merchant.previous_ticket,
            "series": [
                {"month": month, "value": value} for month, value in merchant.series
            ],
            "rows": [_describe_transaction(row) for row in rows],
        }

    @staticmethod
    async def overview() -> dict:
        """
        Everything the merchants page draws.

        Nothing here is stored and nothing is proposed: concentration is a
        property of the history rather than a claim about it, so there is no
        decision to record and no card to put away. Recomputed on every load
        like the patterns page, for the same reason — a ranking cached against
        a re-import would outlive the transactions it ranked.
        """
        transactions = await Transaction.find_all().to_list()
        analysis = concentration(transactions)
        by_id = {str(t.id): t for t in transactions}

        return {
            "months": list(analysis.months),
            "history": list(analysis.history),
            "first": analysis.months[0] if analysis.months else "",
            "last": analysis.months[-1] if analysis.months else "",
            "total": analysis.total,
            "unnamed": analysis.unnamed,
            "unnamed_share": (
                round(analysis.unnamed / analysis.total, 4) if analysis.total else 0.0
            ),
            "count": analysis.count,
            "leaderboard": LEADERBOARD,
            "leaderboard_share": analysis.leaderboard_share,
            "half": analysis.half,
            "four_fifths": analysis.four_fifths,
            "curve": list(analysis.curve),
            "merchants": [
                MerchantService._render(merchant, by_id)
                for merchant in analysis.merchants
            ],
            "categories": [
                {
                    "category": share.category,
                    "total": share.total,
                    "share": share.share,
                    "merchants": share.merchants,
                    "leader": share.leader,
                    "leader_share": share.leader_share,
                    "top_share": share.top_share,
                }
                for share in analysis.categories
            ],
            "window_months": WINDOW_MONTHS,
            "min_category_merchants": MIN_CATEGORY_MERCHANTS,
            "comparable": bool(analysis.previous),
        }
