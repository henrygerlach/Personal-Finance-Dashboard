"""
Recovering the payee of a PayPal debit from a PayPal activity export.

Comdirect truncates the merchant out of many PayPal booking lines, leaving a
payment with no payee that no classifier can read. A PayPal export knows who
was paid. This package joins the two: :mod:`~app.paypal.records` reads an
export in either of PayPal's locales, :mod:`~app.paypal.ledger` works out
which of its rows the bank actually saw and who stood behind each one, and
:mod:`~app.paypal.store` keeps the rows so repeated downloads stay cheap.

The recovered name is fed back into the ordinary classification funnel rather
than carrying a category of its own, so a merchant found this way is cached,
corrected and reported exactly like one Comdirect named itself.
"""

from app.paypal.ledger import (
    DEFAULT_MAX_LAG_DAYS,
    PayPalLedger,
    PayPalMatch,
    Settlement,
)
from app.paypal.records import PayPalFormatError, PayPalRow, parse_export
from app.paypal.store import (
    ImportResult,
    has_records,
    import_export,
    load_ledger,
    settlement_details,
    store_rows,
)

__all__ = [
    "DEFAULT_MAX_LAG_DAYS",
    "ImportResult",
    "PayPalFormatError",
    "PayPalLedger",
    "PayPalMatch",
    "PayPalRow",
    "Settlement",
    "has_records",
    "import_export",
    "load_ledger",
    "parse_export",
    "settlement_details",
    "store_rows",
]
