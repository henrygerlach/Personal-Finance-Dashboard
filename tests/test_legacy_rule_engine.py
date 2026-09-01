"""
Tests for the switched-off rule engine and the pages that drive it.

`app/categorizer.py`, the `/rules` and `/categorize` pages and the legacy
hierarchy are the predecessor of the classifier. They are kept rather than
deleted so the decision can be reversed by flipping ``settings.rules_enabled``
back on — and a reversal is only real if the thing being reverted to still
works. That is the whole reason this file exists; it is not an invitation to
extend any of it.

``tests/test_categorizer.py`` covers matching a rule against a booking line.
What is added here is everything around that: first-match ordering, the
process qualifier, the "only what nobody has filed" pass that stops a
recompute overwriting hand-made categories, and the two routes.
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import routes
from app import services as services_module
from app.categorizer import Categorizer
from app.services import CategorizationService
from tests.fixtures.odm import document

OID = "68af1e2c9b4d5a6f7c8d9e01"


def run(coro):
    return asyncio.run(coro)


def rule(substring, major, minor, process=None):
    return SimpleNamespace(
        substring=substring, major=major, minor=minor, process=process
    )


def tx(text="Auftraggeber: AMAZON", process=None, major="Other", minor="Uncategorized"):
    return SimpleNamespace(
        booking_text=text,
        process=process,
        major_category=major,
        minor_category=minor,
    )


# --- Matching -----------------------------------------------------------------


def test_the_first_matching_rule_wins():
    """
    Rules are ordered and overlapping by nature — 609 of them accumulated
    before the classifier replaced this. Which one answers has to be the first,
    or adding a rule changes the meaning of the ones already there.
    """
    categorizer = Categorizer(
        [
            rule("AMAZON", "Shopping", "General Merchandise"),
            rule("AMAZON", "Leisure & Entertainment", "Streaming & Media"),
        ]
    )

    assert categorizer.apply_rules(tx()) == ("Shopping", "General Merchandise")


def test_matching_ignores_case_on_both_sides():
    """
    Booking lines arrive upper case and rules were typed by hand, so a
    case-sensitive match would silently never fire.
    """
    categorizer = Categorizer([rule("amazon", "Shopping", "General Merchandise")])

    assert categorizer.apply_rules(tx("auftraggeber: Amazon"))[0] == "Shopping"


def test_a_rule_naming_a_process_only_fires_on_that_process():
    """
    The qualifier is what separates a card payment from a direct debit at the
    same merchant, which are frequently two different categories.
    """
    categorizer = Categorizer(
        [rule("AMAZON", "Shopping", "General Merchandise", process="Visa-Umsatz")]
    )

    assert categorizer.apply_rules(tx(process="Visa-Umsatz"))[0] == "Shopping"
    assert categorizer.apply_rules(tx(process="Lastschrift"))[0] == "Other"


def test_a_rule_naming_no_process_fires_on_any_of_them():
    """Most rules do not care, and requiring one would break every existing rule."""
    categorizer = Categorizer([rule("AMAZON", "Shopping", "General Merchandise")])

    assert categorizer.apply_rules(tx(process="anything"))[0] == "Shopping"


def test_a_transaction_no_rule_matches_stays_uncategorized():
    """
    The default has to be the state the "only uncategorized" pass recognises,
    or a recompute never revisits it.
    """
    assert Categorizer([]).apply_rules(tx()) == ("Other", "Uncategorized")


def test_a_transaction_with_no_process_does_not_crash_a_process_rule():
    """The column is absent from some exports, so None is ordinary."""
    categorizer = Categorizer(
        [rule("AMAZON", "Shopping", "General Merchandise", process="Visa-Umsatz")]
    )

    assert categorizer.apply_rules(tx(process=None))[0] == "Other"


# --- Recomputing --------------------------------------------------------------


def test_categorizing_rewrites_the_transactions_in_place():
    categorizer = Categorizer([rule("AMAZON", "Shopping", "General Merchandise")])
    rows = [tx(), tx("Auftraggeber: EDEKA")]

    categorizer.categorize(rows)

    assert rows[0].major_category == "Shopping"
    assert rows[1].major_category == "Other"


def test_only_uncategorized_leaves_a_filed_transaction_alone():
    """
    This is the guard on a recompute. Without it, adding one rule rewrites
    every category anybody ever set by hand.
    """
    categorizer = Categorizer([rule("AMAZON", "Shopping", "General Merchandise")])
    filed = tx(major="Leisure & Entertainment", minor="Streaming & Media")

    categorizer.categorize([filed], only_uncategorized=True)

    assert filed.major_category == "Leisure & Entertainment"


@pytest.mark.parametrize(
    "major,minor",
    [("Other", "Uncategorized"), (None, None), (" other ", " UNCATEGORIZED ")],
)
def test_the_default_state_is_recognised_however_it_is_spelled(major, minor):
    """
    The field is optional on the document and has been written by three
    different versions of this app. A spelling the check does not recognise is
    a transaction a recompute can never reach.
    """
    assert Categorizer._is_uncategorized(tx(major=major, minor=minor)) is True


def test_a_filed_transaction_is_not_mistaken_for_the_default_state():
    assert Categorizer._is_uncategorized(tx(major="Housing", minor="Rent")) is False


# --- The service around it ----------------------------------------------------


@pytest.fixture
def db(monkeypatch):
    transaction_model = document("Transaction", "minor_category")
    monkeypatch.setattr(services_module, "Transaction", transaction_model)
    return transaction_model


def stored(
    db, text="Auftraggeber: AMAZON", major="Other", minor="Uncategorized", **extra
):
    extra.setdefault("date", datetime(2026, 7, 3))
    row = db(
        booking_text=text,
        process=None,
        major_category=major,
        minor_category=minor,
        saved=False,
        **extra,
    )
    row.save = _saver(row)
    db.rows.append(row)
    return row


def _saver(row):
    async def save():
        row.saved = True

    return save


def test_a_recompute_saves_only_what_it_touched(db):
    """
    Writing every transaction back on every rule change is a full collection
    rewrite for a change that may affect one row.
    """
    categorizer = Categorizer([rule("AMAZON", "Shopping", "General Merchandise")])
    unfiled = stored(db)
    filed = stored(db, major="Housing", minor="Rent")

    run(
        CategorizationService.recompute_all_categories(
            categorizer, only_uncategorized=True
        )
    )

    assert unfiled.saved is True
    assert filed.saved is False
    assert filed.major_category == "Housing"


def test_a_full_recompute_touches_everything(db):
    categorizer = Categorizer([rule("AMAZON", "Shopping", "General Merchandise")])
    stored(db)
    stored(db, major="Housing", minor="Rent")

    run(CategorizationService.recompute_all_categories(categorizer))

    assert all(row.saved for row in db.rows)


def test_the_uncategorized_count_is_what_the_badge_shows(db):
    stored(db, minor="Uncategorized")
    stored(db, minor="Uncategorized")
    stored(db, minor="Rent")

    assert run(CategorizationService.get_uncategorized_count()) == 2


def test_the_categorize_page_can_step_past_a_transaction(db):
    """
    The page has to let somebody move on without filing the one in front of
    them, or an undecidable line blocks the whole queue.
    """
    stored(db, text="FIRST", date=datetime(2026, 7, 1))
    stored(db, text="SECOND", date=datetime(2026, 7, 2))

    first = run(CategorizationService.get_uncategorized_transaction())
    second = run(CategorizationService.get_uncategorized_transaction(skip=1))

    assert first.booking_text != second.booking_text


def test_stepping_past_the_end_of_the_queue_returns_nothing(db):
    """The page renders an empty state rather than failing."""
    assert run(CategorizationService.get_uncategorized_transaction(skip=5)) is None


def test_a_negative_skip_is_treated_as_none(db):
    """A negative offset reaching the query is one nobody meant to write."""
    stored(db)

    assert run(CategorizationService.get_uncategorized_transaction(skip=-3)) is not None


# --- The pages ----------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)

    async def count():
        return 0

    monkeypatch.setattr(routes.ReviewService, "count", count)
    monkeypatch.setattr(routes.CategorizationService, "get_uncategorized_count", count)
    return TestClient(app)


@pytest.fixture
def rules_db(monkeypatch):
    rule_model = document("CategoryRule", "id")
    transaction_model = document("Transaction", "id")
    monkeypatch.setattr(routes, "CategoryRule", rule_model)
    monkeypatch.setattr(routes, "Transaction", transaction_model)
    return SimpleNamespace(CategoryRule=rule_model, Transaction=transaction_model)


@pytest.fixture
def templates(monkeypatch):
    from fastapi.responses import HTMLResponse

    rendered = []

    class Fake:
        def TemplateResponse(self, name, context):  # noqa: N802 — mirrors Jinja2Templates
            rendered.append((name, context))
            return HTMLResponse("<html></html>")

    monkeypatch.setattr(routes, "templates", Fake())
    return rendered


def stored_rule(
    db, substring="AMAZON", major="Shopping", minor="General Merchandise", process=None
):
    row = db.CategoryRule(
        substring=substring, major=major, minor=minor, process=process
    )
    db.CategoryRule.rows.append(row)
    return row


def test_the_rules_page_groups_rules_by_major_category(client, rules_db, templates):
    """
    Six hundred rules in one flat list is not a page anybody can use, and the
    grouping is the only structure it has.
    """
    stored_rule(rules_db, "AMAZON", "Shopping", "General Merchandise")
    stored_rule(rules_db, "EDEKA", "Food & Drinks", "Groceries")

    client.get("/rules")

    grouped = templates[0][1]["grouped_rules"]
    assert set(grouped) == {"Shopping", "Food & Drinks"}


def test_the_rules_page_can_be_searched_across_every_field(client, rules_db, templates):
    """
    A rule is found by whichever part the person remembers — the substring,
    either category, or the process it is qualified by.
    """
    stored_rule(rules_db, "AMAZON", "Shopping", "General Merchandise")
    stored_rule(rules_db, "EDEKA", "Food & Drinks", "Groceries", process="Visa-Umsatz")

    for needle, expected in [
        ("amazon", 1),
        ("groceries", 1),
        ("food", 1),
        ("visa", 1),
        ("nothing", 0),
    ]:
        client.get(f"/rules?search={needle}")
        assert len(templates[-1][1]["rules"]) == expected


def test_deleting_a_rule_recomputes_what_it_had_categorized(
    client, rules_db, monkeypatch
):
    """
    A deleted rule leaves its transactions filed under a category nothing
    produces any more, which no later run would ever revisit.
    """
    row = stored_rule(rules_db)
    recomputed = []

    async def recompute(categorizer, **kwargs):
        recomputed.append(categorizer)

    monkeypatch.setattr(
        routes.CategorizationService, "recompute_all_categories", recompute
    )

    reply = client.post(f"/rules/{row.id}/delete", follow_redirects=False)

    assert reply.status_code == 303
    assert rules_db.CategoryRule.rows == []
    assert len(recomputed) == 1


def test_deleting_a_rule_that_is_not_there_is_a_404(client, rules_db):
    assert client.post(f"/rules/{OID}/delete").status_code == 404


def test_saving_a_rule_requires_the_substring_to_be_in_the_booking_text(
    client, rules_db
):
    """
    A rule that matches nothing is invisible: it is saved, it never fires, and
    the transaction it was written for stays uncategorized.
    """
    rules_db.Transaction.rows.append(
        rules_db.Transaction(id=OID, booking_text="Auftraggeber: AMAZON", process=None)
    )

    reply = client.post(
        "/categorize",
        data={
            "transaction_id": OID,
            "substring": "EDEKA",
            "major_category": "Shopping",
            "minor_category": "General Merchandise",
        },
    )

    assert reply.status_code == 422
    assert "part of the booking text" in reply.json()["detail"]


def test_a_numeric_substring_means_the_first_n_words_of_the_line(
    client, rules_db, monkeypatch
):
    """
    A shortcut on the page: typing 2 takes the first two words rather than
    making somebody copy them out of the booking text by hand.
    """
    rules_db.Transaction.rows.append(
        rules_db.Transaction(
            id=OID, booking_text="Auftraggeber: AMAZON EU SARL", process=None
        )
    )

    async def recompute(categorizer, **kwargs):
        return None

    monkeypatch.setattr(
        routes.CategorizationService, "recompute_all_categories", recompute
    )

    client.post(
        "/categorize",
        data={
            "transaction_id": OID,
            "substring": "2",
            "major_category": "Shopping",
            "minor_category": "General Merchandise",
        },
        follow_redirects=False,
    )

    assert rules_db.CategoryRule.rows[0].substring == "Auftraggeber: AMAZON"


def test_an_empty_substring_is_refused(client, rules_db):
    """A rule matching the empty string matches every transaction there is."""
    rules_db.Transaction.rows.append(
        rules_db.Transaction(id=OID, booking_text="AMAZON", process=None)
    )

    reply = client.post(
        "/categorize",
        data={
            "transaction_id": OID,
            "substring": "   ",
            "major_category": "Shopping",
            "minor_category": "General Merchandise",
        },
    )

    assert reply.status_code == 422


def test_saving_a_rule_for_a_transaction_that_is_gone_is_a_404(client, rules_db):
    reply = client.post(
        "/categorize",
        data={
            "transaction_id": OID,
            "substring": "AMAZON",
            "major_category": "Shopping",
            "minor_category": "General Merchandise",
        },
    )

    assert reply.status_code == 404


def test_the_transaction_s_own_process_can_be_borrowed_for_the_rule(
    client, rules_db, monkeypatch
):
    """
    A checkbox on the page, so a rule can be qualified without the person
    having to know what Comdirect calls the transaction type.
    """
    rules_db.Transaction.rows.append(
        rules_db.Transaction(id=OID, booking_text="AMAZON", process="Visa-Umsatz")
    )

    async def recompute(categorizer, **kwargs):
        return None

    monkeypatch.setattr(
        routes.CategorizationService, "recompute_all_categories", recompute
    )

    client.post(
        "/categorize",
        data={
            "transaction_id": OID,
            "substring": "AMAZON",
            "major_category": "Shopping",
            "minor_category": "General Merchandise",
            "use_process": "on",
        },
        follow_redirects=False,
    )

    assert rules_db.CategoryRule.rows[0].process == "Visa-Umsatz"


def test_a_new_rule_only_refiles_what_nobody_has_filed(client, rules_db, monkeypatch):
    """
    Adding a rule must not rewrite the categories somebody set by hand, which
    is the one thing that would make the page unusable.
    """
    rules_db.Transaction.rows.append(
        rules_db.Transaction(id=OID, booking_text="AMAZON", process=None)
    )
    seen = {}

    async def recompute(categorizer, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(
        routes.CategorizationService, "recompute_all_categories", recompute
    )

    client.post(
        "/categorize",
        data={
            "transaction_id": OID,
            "substring": "AMAZON",
            "major_category": "Shopping",
            "minor_category": "General Merchandise",
        },
        follow_redirects=False,
    )

    assert seen == {"only_uncategorized": True}


def test_the_categorizer_can_recompute_and_save_on_its_own(db, monkeypatch):
    """
    A second path to the same job, kept because the maintenance scripts use it
    without going through the service. Switching the rules back on and finding
    it broken would be finding out too late.
    """
    import app.categorizer as categorizer_module

    monkeypatch.setattr(categorizer_module, "Transaction", db)
    categorizer = Categorizer([rule("AMAZON", "Shopping", "General Merchandise")])
    row = stored(db)

    run(categorizer.recompute_categories())

    assert row.major_category == "Shopping"
    assert row.saved is True


def test_a_failing_recompute_is_raised_rather_than_swallowed(db, monkeypatch):
    """
    It rewrites every transaction it touches. A failure part-way through has
    to reach the caller, not be logged and forgotten.
    """
    categorizer = Categorizer([])
    import app.categorizer as categorizer_module

    class Exploding:
        @staticmethod
        def find_all():
            raise RuntimeError("connection lost")

    monkeypatch.setattr(categorizer_module, "Transaction", Exploding)

    with pytest.raises(RuntimeError, match="connection lost"):
        run(categorizer.recompute_categories())
