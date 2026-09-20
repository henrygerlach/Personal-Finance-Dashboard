"""
What changed in the spending, and what reliably did not.
"""


from datetime import datetime

from beanie import PydanticObjectId

from app.categories import MINOR_NEEDS_REVIEW
from app.config import get_logger
from app.models import (
    OVERRIDE_DISMISSED,
    TRIP_INCLUDED,
    PatternDismissal,
    Transaction,
    Trip,
    TripAssignment,
)
from app.normalization import merchant_key
from app.patterns import (
    BASELINE_MONTHS,
    KINDS,
    MIN_SEASON_YEARS,
    RECENT_MONTHS,
    Pattern,
    Schedule,
    analyse,
    before,
    emerged,
)
from app.services.common import _describe_transaction
from app.services.recurring import RecurringService

logger = get_logger(__name__)

logger = get_logger(__name__)


class PatternService:
    """What changed in the spending, and what reliably does not."""

    @staticmethod
    def _schedules(items: list) -> list[Schedule]:
        """
        The recurring page's schedules, as the pattern module wants them.

        Rebuilt from ``RecurringService.overview`` rather than re-detected,
        so the two pages cannot disagree: whether something is a schedule and
        whether it is still running are questions that page has answered, with
        the user's overrides already folded in.
        """
        schedules = []
        for item in items:
            schedules.append(
                Schedule(
                    merchant=item["merchant"],
                    merchant_key=merchant_key(item["merchant"]),
                    period=item["period"],
                    minor=item["minor"] or MINOR_NEEDS_REVIEW,
                    amount=item["amount"],
                    direction=item["direction"],
                    status=item["status"],
                    first_seen=datetime.strptime(item["first_seen"], "%Y-%m-%d"),
                    last_seen=datetime.strptime(item["last_seen"], "%Y-%m-%d"),
                    next_expected=datetime.strptime(item["next_expected"], "%Y-%m-%d"),
                    occurrences=item["occurrences"],
                    transaction_ids=tuple(
                        charge["id"] for charge in reversed(item["charges"])
                    ),
                    price_change=item["price_change"],
                )
            )
        return schedules

    @staticmethod
    async def _trip_names(user_id: PydanticObjectId) -> dict[str, str]:
        """
        Which trip each transaction was assigned to, if any.

        Only inclusions: an excluded transaction is one the user has said is
        *not* the holiday, which is exactly the answer needed when deciding
        whether an expensive month was a holiday.
        """
        trips = {
            str(trip.id): trip.name
            for trip in await Trip.find(Trip.user_id == user_id).to_list()
        }
        assignments = await TripAssignment.find(
            TripAssignment.user_id == user_id,
            TripAssignment.decision == TRIP_INCLUDED,
        ).to_list()
        return {
            a.transaction_id: trips[a.trip_id]
            for a in assignments
            if a.trip_id in trips
        }

    @staticmethod
    def _render(pattern: Pattern, by_id: dict, fresh: frozenset) -> dict:
        """
        One finding as the page needs it, proof included.

        The transactions travel with the finding rather than behind a second
        request, the way the recurring page ships its charges: a claim about
        the money should be openable in one click, and a card whose evidence
        arrives separately is a card that can be read before its evidence
        contradicts it.
        """
        rows = [by_id[tid] for tid in pattern.transaction_ids if tid in by_id]
        return {
            "id": "|".join(pattern.key),
            "new": pattern.key in fresh,
            "kind": pattern.kind,
            "subject": pattern.subject,
            "month": pattern.month,
            "label": pattern.label,
            "category": pattern.category,
            "headline": pattern.headline,
            "facts": pattern.facts,
            "series": [
                {"month": month, "value": value} for month, value in pattern.series
            ],
            "rows": [_describe_transaction(row) for row in rows],
        }

    @staticmethod
    async def overview(user_id: PydanticObjectId) -> dict:
        """
        Everything the patterns page draws.

        Two halves, and the split matters. The *structure* — the committed
        floor, how much of a month is decided before it starts, the shape of a
        year — is always shown, because it is true whether or not anything
        changed. The *findings* are proposals: each one is a claim about the
        history that the user can look at, check against the transactions
        behind it, and put away.
        """
        transactions = await Transaction.find(Transaction.user_id == user_id).to_list()
        recurring = await RecurringService.overview(user_id)
        dismissals = await PatternDismissal.find(
            PatternDismissal.user_id == user_id
        ).to_list()

        # Dismissed runs are not schedules — the user has said so — and ended
        # ones still are: what they cost while they ran is what makes the
        # commitment floor a history rather than a snapshot.
        schedules = PatternService._schedules(
            [
                item
                for item in recurring["items"]
                if item["override"] != OVERRIDE_DISMISSED
            ]
        )
        trip_of = await PatternService._trip_names(user_id)

        analysis = analyse(transactions, schedules=schedules, trip_of=trip_of)

        # The same analysis over the ledger without its newest complete month,
        # so a finding can say whether the last statement is what produced it.
        # Recomputed rather than remembered: findings are derived from the
        # transactions, and a stored first-seen would go stale behind a
        # re-import or a re-classification without ever saying so.
        fresh = emerged(
            analysis,
            analyse(
                before(transactions, analysis.reference),
                schedules=schedules,
                trip_of=trip_of,
            ),
        )

        by_id = {str(t.id): t for t in transactions}
        put_away = {(d.kind, d.subject, d.month) for d in dismissals}

        live, hidden = [], []
        for pattern in analysis.findings:
            rendered = PatternService._render(pattern, by_id, fresh)
            (hidden if pattern.key in put_away else live).append(rendered)

        # New first, then the standing ones, each half still ranked by euros a
        # month. A finding the last statement produced is the one being read
        # for the first time; a large standing finding would otherwise sit
        # above it on every load until it was put away.
        live.sort(key=lambda finding: not finding["new"])

        months = list(analysis.months)
        return {
            "months": months,
            "series": [
                {
                    "month": month,
                    "spend": analysis.spend[index],
                    "income": analysis.income[index],
                    "committed": analysis.committed[index],
                    "fixed": analysis.fixed[index],
                }
                for index, month in enumerate(months)
            ],
            "seasonal": list(analysis.seasonal),
            "findings": live,
            "dismissed": hidden,
            "count": len(live),
            "dismissed_count": len(hidden),
            "floor": analysis.floor,
            # The last month whose statement has actually arrived. Every
            # comparison on the page is measured from here rather than from
            # today, because statements land in batches and today is usually
            # weeks ahead of the data.
            "reference": analysis.reference,
            "recent_months": RECENT_MONTHS,
            "baseline_months": BASELINE_MONTHS,
            "min_season_years": MIN_SEASON_YEARS,
        }

    @staticmethod
    async def dismiss(
        user_id: PydanticObjectId, kind: str, subject: str, month: str
    ) -> bool:
        """
        Put one finding away.

        Keyed on the finding's own facts, so it stays dismissed across the
        recomputation that happens on every page load — and so the same
        category drifting again next year is a new finding that gets offered
        again, which is the point.

        Raises:
            ValueError: The kind is not one this app produces. A dismissal
                that matches nothing would sit in the collection forever.
        """
        if kind not in KINDS:
            raise ValueError(f"{kind!r} is not a kind of pattern")

        existing = await PatternDismissal.find_one(
            PatternDismissal.user_id == user_id,
            PatternDismissal.kind == kind,
            PatternDismissal.subject == subject,
            PatternDismissal.month == month,
        )
        if existing:
            return False
        await PatternDismissal(
            user_id=user_id, kind=kind, subject=subject, month=month
        ).insert()
        return True

    @staticmethod
    async def restore(
        user_id: PydanticObjectId, kind: str, subject: str, month: str
    ) -> bool:
        """Hand one finding back to the page."""
        existing = await PatternDismissal.find_one(
            PatternDismissal.user_id == user_id,
            PatternDismissal.kind == kind,
            PatternDismissal.subject == subject,
            PatternDismissal.month == month,
        )
        if not existing:
            return False
        await existing.delete()
        return True
