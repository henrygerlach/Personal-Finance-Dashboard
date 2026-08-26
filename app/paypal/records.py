"""
Reading a PayPal activity export.

PayPal's CSV is localised: the column names, the date order and the decimal
separator all follow the account's language and country. This module turns any
of those dialects into one :class:`PayPalRow` shape so nothing downstream
has to know which locale a file came from.

Three details are worth stating, because each one silently corrupts the data
if it is guessed wrong:

*Column names.* ``COLUMN_ALIASES`` maps the German and English spellings onto
canonical names. A file whose header matches neither is rejected loudly rather
than parsed into a table of empty strings.

*Decimal separator.* ``"1.234,56"`` and ``"1,234.56"`` are the same amount.
The separator nearest the end wins, but only when one or two digits follow it:
``"1.234"`` is a thousands separator and means 1234, not 1.234.

*Date order.* ``01.02.2024`` is 1 February in a German export and 2 January in
a US one, and no single row can tell the two apart. The order is decided once
per file, from every date in it: a value above twelve in the first position
anywhere proves day-first. Failing that the separator decides, because PayPal
writes dots only in the day-first locales.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime

# --- Column names ------------------------------------------------------------

#: Canonical column -> the spellings PayPal exports it under. Compared after
#: lower-casing and collapsing whitespace, so casing and stray spaces do not
#: matter. Only the columns this package reads are listed; an export carries
#: around forty and the rest are ignored.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("datum", "date"),
    "name": ("name",),
    "kind": ("typ", "type"),
    "status": ("status",),
    "currency": ("währung", "wahrung", "currency"),
    "gross": ("brutto", "gross"),
    "code": ("transaktionscode", "transaction id"),
    "related_code": (
        "zugehöriger transaktionscode",
        "zugehoriger transaktionscode",
        "reference txn id",
    ),
    "item_title": ("artikelbezeichnung", "item title"),
}

#: Columns without which a file cannot be interpreted at all. ``name`` is not
#: among them: the bank-facing rows leave it empty by design, and the merchant
#: is recovered by following ``related_code`` instead.
REQUIRED_COLUMNS = ("date", "kind", "gross", "code")

#: Encodings to try, in order. PayPal serves UTF-8 with a BOM from the web
#: export and Latin-1 from some older account settings; utf-8-sig also reads
#: plain UTF-8, so it is safe to try first.
_ENCODINGS = ("utf-8-sig", "utf-16", "cp1252", "latin-1")


class PayPalFormatError(ValueError):
    """A file that does not look like a PayPal activity export."""


@dataclass(frozen=True)
class PayPalRow:
    """
    One row of a PayPal activity export, as the file has it.

    The stored counterpart is ``app.models.PayPalRecord``; this is the shape
    the reader produces, before anything is persisted or interpreted.

    ``gross`` keeps PayPal's sign convention: negative leaves the PayPal
    balance, positive enters it. ``related_code`` points at the row this one
    belongs to and is the thread the ledger pulls to find a merchant.
    """

    code: str
    date: date
    kind: str
    status: str
    name: str
    currency: str
    gross: float
    related_code: str
    item_title: str


# --- Scalars -----------------------------------------------------------------


def _clean(value: str | None) -> str:
    """Trim and collapse the whitespace PayPal leaves in its fields."""
    return re.sub(r"\s+", " ", (value or "").strip())


def parse_amount(raw: str | None) -> float | None:
    """
    A PayPal amount in either locale, or None if the field is empty.

    The separator closest to the end is the decimal point when one or two
    digits follow it, and a thousands separator otherwise. Every other
    separator is grouping and is dropped.
    """
    text = _clean(raw).replace("\xa0", "").replace(" ", "")
    if not text:
        return None

    negative = text.startswith("-") or (text.startswith("(") and text.endswith(")"))
    text = text.lstrip("+-").strip("()")
    if not text:
        return None

    cut = max(text.rfind("."), text.rfind(","))
    if cut == -1:
        digits, fraction = text, ""
    elif 1 <= len(text) - cut - 1 <= 2 and text[cut + 1 :].isdigit():
        digits, fraction = text[:cut], text[cut + 1 :]
    else:
        digits, fraction = text, ""

    digits = re.sub(r"[.,]", "", digits)
    if not digits.isdigit() and digits != "":
        raise PayPalFormatError(f"Not an amount: {raw!r}")

    value = float(f"{digits or '0'}.{fraction or '0'}")
    return -value if negative else value


_DATE_PATTERNS = ("%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d", "%d-%m-%Y")


def _date_pattern_for(values: list[str]) -> str:
    """
    The date format the whole file is written in.

    Deciding per file rather than per row is what keeps 01.02.2024 and
    03.02.2024 in the same calendar: a single row cannot say which of its
    first two numbers is the month, but a column of them usually can.
    """
    cleaned = [_clean(v) for v in values if _clean(v)]
    if not cleaned:
        return _DATE_PATTERNS[0]

    if all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) for v in cleaned):
        return "%Y-%m-%d"

    parts = [re.split(r"[./-]", v) for v in cleaned]
    if any(len(p) != 3 for p in parts):
        raise PayPalFormatError(f"Unrecognised date: {cleaned[0]!r}")

    first = [int(p[0]) for p in parts if p[0].isdigit()]
    second = [int(p[1]) for p in parts if p[1].isdigit()]
    separator = "." if "." in cleaned[0] else ("/" if "/" in cleaned[0] else "-")

    if any(value > 12 for value in first):
        day_first = True
    elif any(value > 12 for value in second):
        day_first = False
    else:
        # Every date in the file is ambiguous. PayPal writes dots and dashes
        # only in the day-first locales, so the separator is the last clue.
        day_first = separator in {".", "-"}

    order = "%d{0}%m{0}%Y" if day_first else "%m{0}%d{0}%Y"
    return order.format(separator)


def _parse_date(raw: str, pattern: str) -> date:
    text = _clean(raw)
    try:
        return datetime.strptime(text, pattern).date()
    except ValueError as error:
        raise PayPalFormatError(f"Unrecognised date: {raw!r}") from error


# --- Files -------------------------------------------------------------------


def _decode(payload: bytes) -> str:
    for encoding in _ENCODINGS:
        try:
            text = payload.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        # A mis-guessed single-byte codec decodes anything, so require the
        # result to still look like a delimited table.
        if "," in text or ";" in text or "\t" in text:
            return text
    raise PayPalFormatError("Could not decode the file as text")


def _map_header(header: list[str]) -> dict[str, int]:
    """Canonical column name -> its index in this file's header."""
    seen = {_clean(name).lower(): index for index, name in enumerate(header)}
    mapping: dict[str, int] = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in seen:
                mapping[canonical] = seen[alias]
                break

    missing = [column for column in REQUIRED_COLUMNS if column not in mapping]
    if missing:
        raise PayPalFormatError(
            "Not a PayPal activity export — missing column(s): "
            + ", ".join(missing)
            + f". Found: {', '.join(header[:8])}"
        )
    return mapping


def looks_like_export(payload: bytes | str) -> bool:
    """
    Whether this file is a PayPal activity export.

    The upload page takes bank statements and PayPal exports through the same
    control, so something has to tell them apart. The header is the only
    honest signal: a Comdirect statement has no transaction-code column, and a
    PayPal export cannot be read without one.
    """
    try:
        text = _decode(payload) if isinstance(payload, bytes) else payload
        rows = list(csv.reader(io.StringIO(text[:8192])))
    except (PayPalFormatError, csv.Error):
        return False
    for row in rows[:5]:
        if not any(_clean(cell) for cell in row):
            continue
        try:
            _map_header(row)
        except PayPalFormatError:
            return False
        return True
    return False


def parse_export(payload: bytes | str) -> list[PayPalRow]:
    """
    Every row of one PayPal activity export.

    Rows are returned in file order and are not deduplicated: consecutive
    downloads overlap, and it is the store that decides what is already known.

    Raises:
        PayPalFormatError: If the file is not a PayPal activity export, or a
            field in it cannot be read.
    """
    text = _decode(payload) if isinstance(payload, bytes) else payload
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel  # a single-column file sniffs badly; comma is right

    rows = list(csv.reader(io.StringIO(text), dialect))
    rows = [row for row in rows if any(_clean(cell) for cell in row)]
    if not rows:
        return []

    columns = _map_header(rows[0])
    body = rows[1:]

    def cell(row: list[str], column: str) -> str:
        index = columns.get(column)
        if index is None or index >= len(row):
            return ""
        return _clean(row[index])

    pattern = _date_pattern_for([cell(row, "date") for row in body])

    records: list[PayPalRow] = []
    for row in body:
        code = cell(row, "code")
        if not code:
            continue  # a total or note line, not a transaction
        gross = parse_amount(cell(row, "gross"))
        records.append(
            PayPalRow(
                code=code,
                date=_parse_date(cell(row, "date"), pattern),
                kind=cell(row, "kind"),
                status=cell(row, "status"),
                name=cell(row, "name"),
                currency=cell(row, "currency").upper(),
                gross=0.0 if gross is None else gross,
                related_code=cell(row, "related_code"),
                item_title=cell(row, "item_title"),
            )
        )
    return records
