"""
Tests for the Comdirect CSV parser.

The parser is the only thing standing between a bank export and the database,
and everything downstream trusts what it produces. Three properties matter and
each of them is a format quirk rather than a design choice: the export is
German-locale (semicolons, comma decimals, dots for thousands), it holds more
than one account in one file with a fresh header block per account, and it
wraps some rows across two lines. Getting any of those wrong does not raise —
it silently produces a wrong amount or drops a transaction.
"""

from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from app.categories import NEEDS_REVIEW
from app.constants import COMDIRECT_CSV_ENCODING
from app.parser import Parser


@pytest.fixture(autouse=True)
def transaction_stand_in(monkeypatch):
    """
    A ``Transaction`` that needs no database.

    Constructing the real Beanie document asks for its collection, which only
    exists once the app has connected. What the parser is responsible for is
    the values it puts in the fields, so the document is stood in for and the
    values are read back off the stand-in.
    """
    monkeypatch.setattr("app.parser.Transaction", SimpleNamespace)


def csv_bytes(body: str) -> bytes:
    """A CSV in the encoding Comdirect actually writes."""
    return body.encode(COMDIRECT_CSV_ENCODING)


HEADER = (
    '"Buchungstag";"Wertstellung (Valuta)";"Vorgang";"Buchungstext";"Umsatz in EUR";'
)


def export(*rows: str, account: str = "Girokonto") -> bytes:
    """One account section wrapping the given transaction rows."""
    lines = [
        ";",
        f'"Umsätze {account}";"Zeitraum: 01.10.2024 - 31.10.2024";',
        "",
        HEADER,
    ]
    lines.extend(rows)
    return csv_bytes("\n".join(lines))


def csv_row(day, process, text, amount, booked=None):
    """One transaction row, in the column order the export writes them."""
    return f'"{day}";"{booked or day}";"{process}";"{text}";"{amount}";'


DEBIT = "Lastschrift / Belastung"
TRANSFER = "Übertrag / Überweisung"

ROW_AMAZON = csv_row("25.10.2024", DEBIT, "Auftraggeber: AMAZON", "-20,71")
ROW_SALARY = csv_row("28.10.2024", TRANSFER, "Auftraggeber: Gehalt", "1.000,00")


@pytest.fixture
def parser():
    """A parser with no rules; the rule engine is switched off by default."""
    return Parser(rules=[])


# --- The German number format ------------------------------------------------


def test_a_comma_is_the_decimal_point_and_a_dot_is_the_thousands_separator(parser):
    """
    "1.000,00" is one thousand euros, not one. Reading it with the C locale
    gives 1.0 and silently understates a salary by three orders of magnitude —
    a wrong number that looks entirely plausible on a chart.
    """
    transactions = parser.parse(export(ROW_SALARY))

    assert transactions[0].amount == 1000.00


def test_a_negative_amount_stays_negative(parser):
    """Direction is carried by the sign, and every total downstream reads it."""
    transactions = parser.parse(export(ROW_AMAZON))

    assert transactions[0].amount == -20.71


def test_the_booking_date_is_read_day_first(parser):
    """
    German dates are ``%d.%m.%Y``. Parsed month-first, 25.10.2024 is not a
    date at all in some rows and the wrong month in others, which moves a
    transaction between the buckets every monthly chart is built from.
    """
    transactions = parser.parse(export(ROW_AMAZON))

    assert transactions[0].date == datetime(2024, 10, 25)


# --- What a parsed transaction carries ---------------------------------------


def test_the_booking_text_and_process_survive_unchanged(parser):
    """
    Normalization and classification both read these two fields, so the parser
    must not tidy them: the machine text it would strip is exactly what
    ``app.normalization`` uses to recognise a payment processor.
    """
    transactions = parser.parse(export(ROW_AMAZON))

    assert transactions[0].booking_text == "Auftraggeber: AMAZON"
    assert transactions[0].process == "Lastschrift / Belastung"


def test_a_freshly_parsed_transaction_is_unreviewed(parser):
    """
    With the rule engine off nothing categorises at parse time. Arriving as
    Needs Review means a classification that never runs leaves the transaction
    visibly unfiled, rather than silently sitting under a default category
    nobody chose.
    """
    transactions = parser.parse(export(ROW_AMAZON))

    assert transactions[0].major_category == NEEDS_REVIEW[0]
    assert transactions[0].minor_category == NEEDS_REVIEW[1]


def test_the_hash_identifies_a_transaction_by_its_four_facts(parser):
    """
    The hash is the deduplication key for re-imports, and overlapping exports
    are the normal case rather than an accident. Two genuinely different
    transactions must not collide, and the same one imported twice must.
    """
    first = parser.parse(export(ROW_AMAZON))[0]
    again = parser.parse(export(ROW_AMAZON))[0]
    other = parser.parse(export(ROW_SALARY))[0]

    assert first.hash == again.hash
    assert first.hash != other.hash


def test_two_charges_differing_only_in_amount_hash_differently(parser):
    """
    A supermarket visited twice on one day is two transactions, and the amount
    is all that separates them. A hash ignoring it would drop the second as a
    duplicate of the first.
    """
    cheaper = csv_row("25.10.2024", DEBIT, "Auftraggeber: AMAZON", "-20,72")

    assert (
        parser.parse(export(ROW_AMAZON))[0].hash
        != parser.parse(export(cheaper))[0].hash
    )


# --- The shape of the file ---------------------------------------------------


def test_every_account_section_in_one_file_is_parsed(parser):
    """
    Comdirect writes each account as its own header block in a single export.
    Only the last block was read for as long as the buffered rows were not
    flushed when a new "Umsätze" line arrived.
    """
    body = "\n".join(
        [
            ";",
            '"Umsätze Girokonto";"Zeitraum: 01.10.2024 - 31.10.2024";',
            "",
            HEADER,
            ROW_AMAZON,
            "",
            '"Umsätze Visa-Karte";"Zeitraum: 01.10.2024 - 31.10.2024";',
            "",
            HEADER,
            ROW_SALARY,
        ]
    )

    transactions = parser.parse(csv_bytes(body))

    assert len(transactions) == 2
    assert {t.amount for t in transactions} == {-20.71, 1000.00}


def test_parsing_stops_at_the_depot_section(parser):
    """
    The securities depot is a different kind of record with the same column
    names. Reading it produces holdings dressed up as transactions.
    """
    body = "\n".join(
        [
            ";",
            '"Umsätze Girokonto";"Zeitraum: 01.10.2024 - 31.10.2024";',
            "",
            HEADER,
            ROW_AMAZON,
            "",
            '"Umsätze Depot";"Zeitraum: 01.10.2024 - 31.10.2024";',
            "",
            HEADER,
            ROW_SALARY,
        ]
    )

    transactions = parser.parse(csv_bytes(body))

    assert len(transactions) == 1
    assert transactions[0].amount == -20.71


def test_a_row_whose_date_was_wrapped_onto_its_own_line_is_rejoined(parser):
    """
    Some exports put the booking date alone on one line and the rest of the
    record on the next. Left alone that is two unusable rows instead of one
    transaction.
    """
    body = "\n".join(
        [
            ";",
            '"Umsätze Girokonto";"Zeitraum: 01.10.2024 - 31.10.2024";',
            "",
            HEADER,
            '"25.10.2024";',
            csv_row("", DEBIT, "Auftraggeber: AMAZON", "-20,71", booked="25.10.2024"),
        ]
    )

    transactions = parser.parse(csv_bytes(body))

    assert len(transactions) == 1
    assert transactions[0].date == datetime(2024, 10, 25)


def test_empty_rows_between_sections_are_skipped(parser):
    """
    The export is padded with blank and semicolon-only lines. Each one reaching
    the DataFrame is a row of NaN that becomes a transaction with no amount.
    """
    body = "\n".join(
        [
            ";",
            '"Umsätze Girokonto";"Zeitraum: 01.10.2024 - 31.10.2024";',
            "",
            HEADER,
            ROW_AMAZON,
            "",
            ";",
            "",
        ]
    )

    assert len(parser.parse(csv_bytes(body))) == 1


def test_a_file_with_no_transaction_rows_parses_to_nothing(parser):
    """
    A month with no activity is a valid export, and an empty one must be an
    empty list rather than an exception on the upload path.
    """
    assert parser.parse(export()) == []


def test_the_sample_export_in_the_repository_parses(parser):
    """
    The fixture is a real export with its accents in ISO-8859-1. Reading it as
    UTF-8 raises, so this is also the test that the encoding is not guessed.
    """
    transactions = parser.parse("sample_data/sample_umsaetze.csv")

    assert len(transactions) == 5
    assert all(t.hash for t in transactions)


def test_a_path_and_the_same_file_as_bytes_parse_identically(parser):
    """
    The upload route hands over bytes and the scripts hand over a path. They
    are the same file and must not become two behaviours.
    """
    from_path = parser.parse("sample_data/sample_umsaetze.csv")
    with open("sample_data/sample_umsaetze.csv", "rb") as handle:
        from_bytes = parser.parse(handle.read())

    assert [t.hash for t in from_path] == [t.hash for t in from_bytes]


# --- Columns the export does not always carry --------------------------------


def test_a_missing_optional_column_becomes_none_rather_than_an_error(parser):
    """
    "Vorgang" is absent from some exports. The classifier treats it as a hint
    and copes without it, so a file lacking it has to parse rather than fail
    the whole upload.
    """
    header = '"Buchungstag";"Buchungstext";"Umsatz in EUR";'
    body = "\n".join(
        [
            ";",
            '"Umsätze Girokonto";"Zeitraum: 01.10.2024 - 31.10.2024";',
            "",
            header,
            '"25.10.2024";"Auftraggeber: AMAZON";"-20,71";',
        ]
    )

    transactions = parser.parse(csv_bytes(body))

    assert transactions[0].process is None


def test_the_alternative_name_for_the_booking_text_column_is_accepted(parser):
    """
    Comdirect labels the same field "Buchungstext" on the giro export and
    "Bezeichnung" on the card export. Only recognising one leaves every
    transaction from the other with an empty merchant.
    """
    header = '"Buchungstag";"Vorgang";"Bezeichnung";"Umsatz in EUR";'
    body = "\n".join(
        [
            ";",
            '"Umsätze Visa-Karte";"Zeitraum: 01.10.2024 - 31.10.2024";',
            "",
            header,
            '"25.10.2024";"Visa-Umsatz";" Lieferando";"-15,56";',
        ]
    )

    transactions = parser.parse(csv_bytes(body))

    assert transactions[0].booking_text == " Lieferando"


# --- The rule engine switch --------------------------------------------------


def test_the_rule_engine_runs_only_when_it_is_switched_on(monkeypatch, parser):
    """
    The rules are the switched-off predecessor. Leaving them applied at parse
    time would file transactions under the legacy taxonomy before the
    classifier ever sees them, which is the one thing the switch exists to
    prevent.
    """
    called = []
    monkeypatch.setattr(
        parser.categorizer,
        "categorize",
        lambda transactions: called.append(transactions),
    )

    monkeypatch.setattr("app.parser.settings.rules_enabled", False)
    parser.parse(export(ROW_AMAZON))
    assert called == []

    monkeypatch.setattr("app.parser.settings.rules_enabled", True)
    parser.parse(export(ROW_AMAZON))
    assert len(called) == 1


# --- The hash helper on its own ----------------------------------------------


def test_the_hash_is_stable_across_runs():
    """
    Deduplication compares against hashes already in the database, so the same
    row must produce the same hash next month and after a restart. Anything
    derived from object identity or a random seed would re-import everything.
    """
    row = {
        "date": pd.Timestamp("2024-10-25"),
        "amount": -20.71,
        "booking_text": "Auftraggeber: AMAZON",
        "process": "Lastschrift / Belastung",
    }

    assert Parser._generate_hash(row) == Parser._generate_hash(dict(row))
    assert len(Parser._generate_hash(row)) == 32


def test_the_same_charge_twice_in_one_day_is_two_transactions(parser):
    """
    Two identical fares, two identical tickets, the same coffee twice: the
    four facts the hash is built from agree on all of them, and the hash is a
    unique index the importer skips on. Giving both rows one hash files the
    second as a re-import of the first and loses it.
    """
    transactions = parser.parse(export(ROW_AMAZON, ROW_AMAZON))

    assert len(transactions) == 2
    assert transactions[0].hash != transactions[1].hash


def test_identical_charges_in_two_account_sections_stay_apart(parser):
    """
    The counter separating identical rows belongs to the file rather than to
    one account block, because Comdirect writes each account as its own
    section and the same charge can appear in two of them.
    """
    girokonto = export(ROW_AMAZON).decode(COMDIRECT_CSV_ENCODING)
    visa = export(ROW_AMAZON, account="Visa-Karte").decode(COMDIRECT_CSV_ENCODING)

    transactions = parser.parse(csv_bytes(girokonto + "\n" + visa))

    assert len(transactions) == 2
    assert transactions[0].hash != transactions[1].hash


def test_a_re_import_of_a_repeated_charge_matches_the_first_import(parser):
    """
    Separating the second charge is only worth anything if the separation is
    reproducible. The same file read next month must yield the same two
    hashes, or the pair re-imports as two more transactions every time.
    """
    first = [t.hash for t in parser.parse(export(ROW_AMAZON, ROW_AMAZON))]
    again = [t.hash for t in parser.parse(export(ROW_AMAZON, ROW_AMAZON))]

    assert first == again


def test_a_lone_transaction_keeps_the_hash_it_already_has():
    """
    Every transaction in the database was hashed from the four facts alone.
    If the occurrence counter changed the hash of the first of a set, every
    stored transaction would stop matching its own export and the whole
    history would import a second time.
    """
    row = {
        "date": pd.Timestamp("2024-10-25"),
        "amount": -20.71,
        "booking_text": "Auftraggeber: AMAZON",
        "process": "Lastschrift / Belastung",
    }

    assert Parser._generate_hash(row, occurrence=0) == Parser._generate_hash(row)
    assert Parser._generate_hash(row, occurrence=1) != Parser._generate_hash(row)
