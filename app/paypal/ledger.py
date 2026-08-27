"""
Recovering the merchant behind a PayPal debit on the bank statement.

Comdirect truncates the merchant out of many PayPal lines, leaving
``PP.5189.PP . , Ihr Ei nkauf bei Ref. ...`` — a payment with no payee. The
PayPal export has the payee; this module is the join between the two.

**The shape of the data.** One PayPal purchase is at least two rows. The
payment names the merchant and leaves the PayPal balance. Alongside it sits a
*settlement*: the row that moves the money between PayPal and the bank, which
is the only one of the pair the bank ever sees. The settlement carries no
merchant name, only a ``related_code`` pointing back at the payment. Following
that pointer is what turns a bare bank debit into "Spotify AB".

The pointer is sometimes followed more than once. A refund arrives as a
withdrawal whose ``related_code`` names the refund row, and only that row
names the merchant, so the walk continues until a name appears or
:data:`_MAX_HOPS` is spent.

**When the pointer is missing.** "Pay in 30 days", PayPal Credit and their
siblings break the chain outright. PayPal advances the money at the till, so
the purchase never touches the bank; a month later it collects, and *that* is
what the bank sees. The collection names PayPal rather than the shop and points
at nothing, so the two are joined only by being the same amount. An unlinked
refund arrives the same way. Where there is no pointer the ledger therefore
falls back to matching the amount against the balance movements around it —
thinner evidence, used only in its absence and flagged on the settlement.

PayPal itself is never accepted as the payee. It is the processor standing
between the two parties, which is the same rule :mod:`app.normalization`
applies when it strips the processor out of a booking text.

**Matching a settlement to a bank line.** Nothing links the two by identity:
the reference Comdirect prints is a SEPA mandate, not a PayPal transaction
code, and the export has no column carrying it. What is left is the amount,
the direction and the delay. Amounts must agree to the cent, the direction has
to be the one the settlement implies, and the bank books between zero and
:data:`DEFAULT_MAX_LAG_DAYS` days after PayPal moves — never before, which is
why the window is one-sided.

That leaves genuine ties: two people paid ten euros each within three days
resolve to two candidates and no way to choose. A tie returns no match rather
than a guess, and the caller keeps the behaviour it already had.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Sequence

from app.paypal.records import PayPalRow

# --- Transaction kinds -------------------------------------------------------


def _normalise_kind(kind: str) -> str:
    return " ".join(kind.lower().split())


#: Kinds that pull money out of the bank and into PayPal. The bank shows these
#: as a debit. PayPal marks them "pending" in every export seen so far and
#: never settles them, so status is deliberately not part of the test.
BANK_TO_PAYPAL_KINDS = frozenset(
    _normalise_kind(kind)
    for kind in (
        "Bankgutschrift auf PayPal-Konto",
        "Bank Deposit to PP Account",
        "Bank Deposit to PayPal Account",
    )
)

#: Kinds that move money from PayPal back to the bank. The bank shows these as
#: a credit — a withdrawal of balance, or the far end of a refund.
PAYPAL_TO_BANK_KINDS = frozenset(
    _normalise_kind(kind)
    for kind in (
        "Von Nutzer eingeleitete Abbuchung",
        "Allgemeine Abbuchung",
        "User Initiated Withdrawal",
        "General Withdrawal",
        "Withdraw Funds to Bank Account",
        "General Withdrawal from PayPal Account",
    )
)

#: Every kind that settles against the bank. An advance of credit is never one
#: of these, which is what keeps the deferred-payment search off the ordinary
#: funding rows.
SETTLEMENT_KINDS = BANK_TO_PAYPAL_KINDS | PAYPAL_TO_BANK_KINDS

#: PayPal's own corporate names, as they appear in the ``Name`` column:
#: "PayPal", "PayPal (Europe) S.a r.l. et Cie, S.C.A." and so on. Anchored at
#: the start so a shop that merely mentions PayPal is not caught by it.
_PAYPAL_ITSELF = re.compile(r"^\s*paypal\b", re.IGNORECASE)

#: How far back an unlinked settlement looks for a balance movement of its own
#: size. An unlinked refund is same-day and "Pay in 30 days" is a month, so
#: 120 days covers the longer credit plans while refusing to reach back years
#: for an unrelated purchase that happened to cost the same to the cent.
LINK_BY_AMOUNT_MAX_DAYS = 120

#: How far a ``related_code`` chain is followed before giving up. Two hops
#: covers the longest real chain (withdrawal -> refund -> merchant); the limit
#: exists to stop a cycle, not to express a rule.
_MAX_HOPS = 4

#: The bank books a PayPal settlement one to eight days later, and most often
#: within four. Five keeps 148 of the 155 truncated lines in one dataset
#: unambiguous; widening it to eight only turns matches into ties.
DEFAULT_MAX_LAG_DAYS = 5


def _is_paypal_itself(name: str) -> bool:
    """Whether this name is PayPal rather than somebody PayPal paid."""
    return bool(_PAYPAL_ITSELF.match(name or ""))


@dataclass(frozen=True)
class Settlement:
    """One movement of money between PayPal and the linked bank account."""

    code: str
    date: date
    #: Always positive: the size of the movement, with ``direction`` carrying
    #: the sign the bank would show.
    amount: float
    currency: str
    #: ``-1`` when the bank is debited, ``+1`` when it is credited.
    direction: int
    #: The merchant or person behind the movement, empty when there is none —
    #: a plain transfer of balance to one's own account has no counterparty.
    counterparty: str
    #: What was bought, when PayPal recorded it. Context for a reviewer only.
    item_title: str
    #: The payment the counterparty came from, for auditing a match.
    source_code: str
    #: The two sides of the payment and whatever the payer wrote on it, taken
    #: from the same row the counterparty came from. Context for a reviewer
    #: only, never evidence: a person-to-person payment often carries nothing
    #: but its message, and just as often carries nothing at all.
    sender: str = ""
    receiver: str = ""
    subject: str = ""
    note: str = ""
    #: True when the payee was found by matching amounts rather than by
    #: following this settlement's own pointer. Weaker evidence, kept visible
    #: so these can be reviewed as a group. See ``_payee_by_amount``.
    linked_by_amount: bool = False


@dataclass(frozen=True)
class PayPalMatch:
    """The PayPal settlement behind one bank transaction."""

    counterparty: str
    settlement: Settlement
    #: Days between the PayPal movement and the bank booking it.
    lag_days: int


class PayPalLedger:
    """
    The PayPal settlements a bank statement can be matched against.

    Built once from the records and then queried per transaction. Settlements
    are indexed by amount in cents, so a lookup touches only the handful of
    movements of exactly that size rather than the whole history.
    """

    def __init__(
        self,
        records: Iterable[PayPalRow],
        *,
        currency: str = "EUR",
        max_lag_days: int = DEFAULT_MAX_LAG_DAYS,
    ) -> None:
        self.currency = currency.upper()
        self.max_lag_days = max_lag_days
        self._by_code = {record.code: record for record in records if record.code}
        self._settlements = self._build_settlements()
        self._index: dict[int, list[Settlement]] = defaultdict(list)
        for settlement in self._settlements:
            self._index[round(settlement.amount * 100)].append(settlement)
        for bucket in self._index.values():
            bucket.sort(key=lambda s: s.date)
        self._dates: dict[int, list[date]] = {
            cents: [s.date for s in bucket] for cents, bucket in self._index.items()
        }
        # Built on first use: only the review page looks a settlement up by
        # its own code, and every other caller searches by amount and date.
        self._by_settlement_code: dict[str, Settlement] | None = None

    # --- Construction --------------------------------------------------------

    def _build_settlements(self) -> list[Settlement]:
        settlements: list[Settlement] = []
        for record in self._by_code.values():
            kind = _normalise_kind(record.kind)
            if kind in BANK_TO_PAYPAL_KINDS:
                direction = -1
            elif kind in PAYPAL_TO_BANK_KINDS:
                direction = 1
            else:
                continue
            if record.currency and record.currency != self.currency:
                continue

            source, by_amount = self._counterparty_of(record)
            settlements.append(
                Settlement(
                    code=record.code,
                    date=record.date,
                    amount=abs(record.gross),
                    currency=record.currency or self.currency,
                    direction=direction,
                    counterparty=source.name if source else "",
                    item_title=source.item_title if source else record.item_title,
                    source_code=source.code if source else record.code,
                    linked_by_amount=by_amount,
                    # The settlement is the bank-facing half and says nothing
                    # about who was paid or why; the row that named the
                    # counterparty is the one carrying both.
                    sender=(source or record).sender,
                    receiver=(source or record).receiver,
                    subject=(source or record).subject,
                    note=(source or record).note,
                )
            )
        return settlements

    def _counterparty_of(self, record: PayPalRow) -> tuple[PayPalRow | None, bool]:
        """
        The row naming whoever was actually paid, and whether it took a detour.

        The pointer is tried first, and is the evidence to prefer. Only when
        it leads nowhere — or no further than PayPal itself — is the weaker
        amount match used.
        """
        found = self._walk_to_name(record)
        if found is not None:
            return found, False
        return self._payee_by_amount(record), True

    def _walk_to_name(self, record: PayPalRow) -> PayPalRow | None:
        """
        Follow ``related_code`` until a row names someone who is not PayPal.

        A settlement never names the merchant itself. Its pointer usually
        reaches the payment in one hop, but a refunded purchase needs two, so
        the walk continues rather than reading only the first link. A row
        naming PayPal is stepped over, not accepted: PayPal is the processor
        standing between the two parties, never one of them.
        """
        seen = {record.code}
        current = record
        for _ in range(_MAX_HOPS):
            next_code = current.related_code
            if not next_code or next_code in seen:
                return None
            seen.add(next_code)
            nxt = self._by_code.get(next_code)
            if nxt is None:
                return None
            current = nxt
            if current.name and not _is_paypal_itself(current.name):
                return current
        return None

    def _named_party(self, record: PayPalRow) -> PayPalRow | None:
        """
        Whoever this row is about: the party it names, or the one it points at.

        The two unlinked shapes differ here. A refund names its shop outright,
        while an advance of credit names nobody and has to be followed to the
        purchase it paid for.
        """
        if record.name and not _is_paypal_itself(record.name):
            return record
        return self._walk_to_name(record)

    def _payee_by_amount(self, settlement: PayPalRow) -> PayPalRow | None:
        """
        The party behind a settlement PayPal left unlinked, found by amount.

        Two real cases arrive with no usable pointer, and both are a balance
        movement of exactly the settled amount sitting a little earlier:

        *Deferred payment.* Under "Pay in 30 days" and PayPal Credit the
        purchase is settled from PayPal's money, so the bank sees nothing at
        the time. Weeks later PayPal collects; that collection names PayPal
        and points at nothing, and the advance is what leads back to the shop.

        *An unlinked refund.* A shop refunds into the balance and the balance
        is withdrawn to the bank, with no pointer joining the two.

        This is thinner evidence than a pointer, so it is only used when there
        is none, and it is recorded as ``linked_by_amount`` on the settlement.
        Candidates that disagree about who was paid resolve to no answer, on
        the same reasoning as an ambiguous settlement: an amount alone cannot
        choose between two shops.
        """
        cents = round(abs(settlement.gross) * 100)
        earliest = settlement.date - timedelta(days=LINK_BY_AMOUNT_MAX_DAYS)

        candidates: list[tuple[date, PayPalRow]] = []
        for record in self._by_code.values():
            if record.code == settlement.code or record.gross <= 0:
                continue
            if _normalise_kind(record.kind) in SETTLEMENT_KINDS:
                continue
            if round(record.gross * 100) != cents:
                continue
            if not earliest <= record.date <= settlement.date:
                continue
            payee = self._named_party(record)
            if payee is not None:
                candidates.append((record.date, payee))

        if not candidates:
            return None
        if len({payee.name.casefold() for _, payee in candidates}) > 1:
            return None
        # The most recent advance, when one shop was paid this way twice.
        return max(candidates, key=lambda pair: pair[0])[1]

    # --- Queries -------------------------------------------------------------

    @property
    def settlements(self) -> Sequence[Settlement]:
        return tuple(self._settlements)

    def settlement_for(self, code: str) -> Settlement | None:
        """
        The settlement with this transaction code, or None.

        A transaction that has already been matched records the code it was
        matched to, so this is how a caller gets back to what PayPal knew
        about it without repeating the search that found it.
        """
        if not code:
            return None
        if self._by_settlement_code is None:
            self._by_settlement_code = {s.code: s for s in self._settlements}
        return self._by_settlement_code.get(code)

    def candidates(self, amount: float, booked_on: date) -> list[Settlement]:
        """
        Every settlement that could be the one behind this bank line.

        ``amount`` keeps the bank's sign: negative for a debit. The direction
        it implies has to be the direction of the settlement, so a refund is
        never matched against a purchase of the same size.
        """
        direction = -1 if amount < 0 else 1
        bucket = self._index.get(round(abs(amount) * 100))
        if not bucket:
            return []

        dates = self._dates[round(abs(amount) * 100)]
        earliest = booked_on - timedelta(days=self.max_lag_days)
        lo = bisect_left(dates, earliest)
        hi = bisect_right(dates, booked_on)
        return [s for s in bucket[lo:hi] if s.direction == direction]

    def resolve(self, amount: float, booked_on: date) -> PayPalMatch | None:
        """
        The counterparty behind one bank transaction, when it is unambiguous.

        Returns None when nothing matches, when the only matches have no
        counterparty to offer, or when several different counterparties fit —
        a tie is reported as no answer rather than resolved by picking the
        nearest, which would be a guess wearing a date as justification.
        """
        named = [s for s in self.candidates(amount, booked_on) if s.counterparty]
        if not named:
            return None
        if len({s.counterparty.casefold() for s in named}) > 1:
            return None

        best = min(named, key=lambda s: (booked_on - s.date).days)
        return PayPalMatch(
            counterparty=best.counterparty,
            settlement=best,
            lag_days=(booked_on - best.date).days,
        )
