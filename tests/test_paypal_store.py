"""
Tests for storing PayPal exports and rebuilding a ledger from them.

Importing is defined as "make sure these rows are known", not "add these
rows", because consecutive downloads overlap heavily — seven exports of one
account shared 130 rows. That makes two properties load-bearing: re-importing
a file must change nothing, and a stored row must never be rewritten, because
the reading that was current when a bank statement was matched is the reading
those matches were made against.

The one deliberate exception is a field nothing matches on. Rows stored before
the sender and message columns were read have them blank, and a blank that can
never be filled is a message the user can never see.

``app.paypal.ledger`` and ``app.paypal.records`` are tested on their own; the
documents here are stood in for by :mod:`tests.fixtures.odm`.
"""

import asyncio
from datetime import date, datetime

import pytest

from app.paypal import store as store_module
from app.paypal.records import PayPalRow
from app.paypal.store import (
    ImportResult,
    has_records,
    import_export,
    load_ledger,
    settlement_details,
    store_rows,
)
from tests.fixtures.odm import document


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def records(monkeypatch):
    record_model = document("PayPalRecord", "code")
    monkeypatch.setattr(store_module, "PayPalRecord", record_model)
    return record_model


def row(code="1A2B", kind="Allgemeine Zahlung", gross=-9.99, related="", **extra):
    defaults = dict(
        code=code,
        date=date(2026, 7, 3),
        kind=kind,
        status="Abgeschlossen",
        name="Spotify AB",
        currency="EUR",
        gross=gross,
        related_code=related,
        item_title="",
        sender="",
        receiver="",
        subject="",
        note="",
    )
    defaults.update(extra)
    return PayPalRow(**defaults)


# --- Importing is idempotent --------------------------------------------------


def test_a_new_row_is_stored(records):
    result = run(store_rows([row()]))

    assert result == ImportResult(read=1, added=1, already_known=0, detailed=0)
    assert len(records.rows) == 1


def test_importing_the_same_export_twice_adds_nothing(records):
    """
    Downloads overlap by design, and the upload page accepts the same file
    again. A second import that doubled every row would silently double every
    match made from them.
    """
    run(store_rows([row()]))
    result = run(store_rows([row()]))

    assert result.added == 0
    assert result.already_known == 1
    assert len(records.rows) == 1


def test_a_wider_export_costs_only_its_new_rows(records):
    """
    Re-downloading a longer date range is the normal way to get last month's
    rows, so the overlap has to be free.
    """
    run(store_rows([row(code="OLD")]))

    result = run(store_rows([row(code="OLD"), row(code="NEW")]))

    assert (result.added, result.already_known) == (1, 1)


def test_a_code_repeated_inside_one_file_is_stored_once(records):
    """
    A single export can list the same code twice. Without this the insert
    carries a duplicate and the unique index rejects the whole batch.
    """
    result = run(store_rows([row(code="X"), row(code="X")]))

    assert result.added == 1
    assert len(records.rows) == 1


def test_importing_nothing_is_not_a_query(records):
    """
    An empty export is a valid file, and an empty ``$in`` is a query that asks
    the database for nothing at some expense.
    """
    assert run(store_rows([])) == ImportResult()


# --- A stored row is not rewritten --------------------------------------------


def test_a_stored_row_keeps_its_original_reading(records):
    """
    The export is a historical record. If PayPal reports a row differently
    later, the earlier reading is the one the bank statement was matched
    against, and overwriting it silently re-points matches already made.
    """
    run(store_rows([row(code="X", gross=-9.99)]))

    run(store_rows([row(code="X", gross=-99.99, name="Somebody Else")]))

    assert records.rows[0].gross == -9.99
    assert records.rows[0].name == "Spotify AB"


def test_an_empty_message_field_is_filled_in_later(records):
    """
    Rows stored before those columns were read have them blank. Nothing
    matches on a message, so writing one cannot re-point anything — and a
    blank that can never be filled is a note the user can never see.
    """
    run(store_rows([row(code="X")]))

    result = run(store_rows([row(code="X", subject="Dinner", sender="a@example.com")]))

    assert result.detailed == 1
    assert records.rows[0].subject == "Dinner"
    assert records.rows[0].sender == "a@example.com"


def test_a_message_that_is_already_there_is_left_alone(records):
    """The exception is for empty fields only, or it is not an exception."""
    run(store_rows([row(code="X", subject="Dinner")]))

    result = run(store_rows([row(code="X", subject="Something else")]))

    assert result.detailed == 0
    assert records.rows[0].subject == "Dinner"


def test_an_export_carrying_no_new_detail_reports_none(records):
    """The count is what the import summary tells the user it did."""
    run(store_rows([row(code="X", subject="Dinner")]))

    assert run(store_rows([row(code="X", subject="Dinner")])).detailed == 0


# --- What the import says it did ----------------------------------------------


def test_the_summary_names_the_three_numbers():
    """It is logged and shown after an upload, so it has to read as a sentence."""
    text = ImportResult(read=10, added=3, already_known=7).summary()

    assert text == "10 rows read, 3 added, 7 already known"


def test_the_summary_mentions_filled_in_detail_only_when_there_was_some():
    """A clause reading "0 filled in" on every ordinary import is noise."""
    assert "filled in" not in ImportResult(read=1, added=1).summary()
    assert "2 filled in" in ImportResult(read=1, already_known=1, detailed=2).summary()


# --- Reading an export end to end ---------------------------------------------


EXPORT = (
    '"Datum","Uhrzeit","Zeitzone","Name","Typ","Status","Währung","Brutto",'
    '"Transaktionscode","Zugehöriger Transaktionscode"\n'
    '"03.07.2026","10:00:00","CEST","Spotify AB","Allgemeine Zahlung",'
    '"Abgeschlossen","EUR","-9,99","PAY1",""\n'
)


def test_an_export_is_read_and_stored(records):
    """
    The upload page hands over bytes. This is the only path that exercises the
    reader and the store together.
    """
    result = run(import_export(EXPORT.encode("utf-8")))

    assert result.added == 1
    assert records.rows[0].code == "PAY1"
    assert records.rows[0].name == "Spotify AB"


def test_a_stored_date_is_a_datetime_rather_than_a_date(records):
    """
    The export carries a day and the document stores a datetime. Mixing the
    two is a comparison that raises inside a query.
    """
    run(store_rows([row()]))

    assert isinstance(records.rows[0].date, datetime)


# --- The ledger ---------------------------------------------------------------


def test_the_ledger_is_built_from_what_is_stored(records):
    """
    Rebuilt per request rather than cached: it is a few hundred settlements,
    and a stale one costs more in confusion than a fresh one costs in time.
    """
    run(
        store_rows(
            [
                row(code="PAY1", gross=-9.99, name="Spotify AB"),
                row(
                    code="SET1",
                    kind="Allgemeines Wechselkurs-Guthaben",
                    gross=9.99,
                    related="PAY1",
                    name="",
                ),
            ]
        )
    )

    ledger = run(load_ledger())

    assert ledger is not None


def test_a_ledger_over_no_rows_is_still_a_ledger(records):
    """
    Nothing has been imported on a fresh install, and the classifier consults
    the ledger before it knows that.
    """
    assert run(load_ledger()) is not None


def test_whether_anything_has_been_imported_yet(records):
    """
    The funnel skips the PayPal step entirely when there is nothing to join
    against, so this is the question it asks first.
    """
    assert run(has_records()) is False

    run(store_rows([row()]))

    assert run(has_records()) is True


# --- Getting back to a settlement ---------------------------------------------


def test_asking_about_no_codes_does_not_build_a_ledger(records):
    """
    The review page calls this for every screenful, and most transactions
    carry no settlement code at all.
    """
    assert run(settlement_details([])) == {}
    assert run(settlement_details([None, ""])) == {}


def test_a_code_the_export_does_not_cover_is_simply_absent(records):
    """
    An export can be narrower than the statement. A missing code is ordinary,
    not an error, and the page shows the line without the extra detail.
    """
    run(store_rows([row(code="PAY1")]))

    assert run(settlement_details(["NOT-IN-EXPORT"])) == {}
