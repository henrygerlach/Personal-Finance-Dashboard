"""
Tests for merchant concentration.

The page these back answers a question the category charts cannot: not how
much went on food, but how few shops it went to. Everything here is therefore
about grouping and about what is kept out of a ranking — a merchant counted
twice, or a transfer counted at all, changes the answer to "who gets the
money" more than any threshold does.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.merchants import analyse

FOOD = ("Food & Drink", "Groceries")
TRANSPORT = ("Transportation", "Public Transit")


def payment(
    tid,
    year,
    month,
    day,
    amount,
    merchant="EDEKA",
    category=FOOD,
    counterparty=None,
):
    major, minor = category
    return SimpleNamespace(
        id=tid,
        date=datetime(year, month, day),
        amount=amount,
        booking_text=merchant,
        process="Kartenzahlung",
        major_category=major,
        minor_category=minor,
        paypal_counterparty=counterparty,
    )


def ledger(*extra, months=30, merchants=(("EDEKA", -40.0), ("REWE", -10.0))):
    """
    A ledger with whole months either side of the ranking window.

    Thirty months is enough for the window, the window before it and the
    partial months at both ends that :func:`app.patterns.complete_months`
    drops, so a test that cares about the comparison window has one.
    """
    rows = []
    index = 0
    for offset in range(months):
        year, month = divmod(offset, 12)
        year, month = 2023 + year, month + 1
        for name, amount in merchants:
            for day in (2, 27):
                index += 1
                rows.append(payment(f"{name}-{index}", year, month, day, amount, name))
    return rows + list(extra)


def named(analysis):
    return [merchant.name for merchant in analysis.merchants]


# --- The ranking -------------------------------------------------------------


def test_merchants_are_ranked_by_what_they_were_paid():
    analysis = analyse(ledger())
    assert named(analysis) == ["EDEKA", "REWE"]
    assert analysis.merchants[0].total > analysis.merchants[1].total


def test_only_the_window_is_ranked():
    """
    A merchant that stopped two years ago is history, not a habit.

    The window is what makes the ranking answer "who gets the money" in the
    present tense; without it the page would rank a subscription cancelled in
    the first month of the file above one paid every month since.
    """
    gone = [payment(f"old-{i}", 2023, 3, 2, -900.0, "BLOCKBUSTER") for i in range(4)]
    analysis = analyse(ledger(*gone))
    assert "BLOCKBUSTER" not in named(analysis)


def test_the_share_of_each_merchant_is_of_everything_spent():
    analysis = analyse(ledger())
    total = sum(merchant.share for merchant in analysis.merchants)
    assert total == pytest.approx(1.0, abs=0.001)


def test_a_visit_is_counted_and_priced():
    analysis = analyse(ledger())
    edeka = analysis.merchants[0]
    assert edeka.visits == 24
    assert edeka.ticket == pytest.approx(40.0)


# --- What counts as one merchant ---------------------------------------------


def test_two_spellings_of_one_merchant_are_ranked_once():
    """
    Comdirect wraps mid-word, so one shop arrives as two names.

    The canonical map folds the longer spelling onto the shorter one wherever
    the shorter is itself in the file. A ranking that missed this would split
    the largest merchant in half and report the second-largest as the leader.
    """
    wrapped = [
        payment(f"wrap-{i}", 2025, 3, 5, -60.0, "EDEKA BOECKER HAMBURG")
        for i in range(6)
    ]
    analysis = analyse(ledger(*wrapped, merchants=(("EDEKA BOECKER", -40.0),)))
    assert named(analysis).count("EDEKA BOECKER") == 1
    assert "EDEKA BOECKER HAMBURG" not in named(analysis)


def test_the_recovered_paypal_payee_is_ranked_as_itself():
    """
    Comdirect's own line for a PayPal debit says only "PayPal".

    Every one of them would collapse into a single merchant holding a large
    share of the ledger — the most misleading row the page could draw, because
    it would look exactly like real concentration.
    """
    rows = [
        payment(
            f"pp-{i}",
            2025,
            4,
            9,
            -55.0,
            "PayPal Europe S.a.r.l. et Cie S.C.A",
            counterparty="Steam Games",
        )
        for i in range(6)
    ]
    analysis = analyse(ledger(*rows))
    assert "STEAM GAMES" in named(analysis)
    assert "PAYPAL" not in named(analysis)


def test_a_merchant_paid_two_ways_is_one_merchant():
    """
    The same shop reached by card and through PayPal is one relationship.

    The recovered payee goes through the same normalizer as a booking line, so
    the two spellings differ in case alone and would otherwise be ranked as
    two merchants each holding half the truth.
    """
    rows = [
        payment(f"card-{i}", 2025, 5, 6, -30.0, "STEAM GAMES") for i in range(3)
    ] + [
        payment(f"pp-{i}", 2025, 5, 7, -30.0, "PayPal", counterparty="Steam Games")
        for i in range(3)
    ]
    analysis = analyse(ledger(*rows))
    assert named(analysis).count("STEAM GAMES") == 1


# --- What stays out ----------------------------------------------------------


def test_transfers_are_not_merchants():
    """
    Moving money is not paying somebody.

    A savings contribution is the largest single line in many months, and a
    ranking that counted it would put the user's own account at the top of a
    list of merchants.
    """
    rows = [
        payment(
            f"save-{i}",
            2025,
            m,
            10,
            -800.0,
            "TAGESGELD",
            category=("Transfers & Savings", "Savings Contribution"),
        )
        for i, m in enumerate((1, 2, 3, 4))
    ]
    analysis = analyse(ledger(*rows))
    assert "TAGESGELD" not in named(analysis)


def test_income_is_not_a_merchant():
    rows = [
        payment(
            f"pay-{i}",
            2025,
            m,
            1,
            3200.0,
            "ARBEITGEBER",
            category=("Income", "Salary & Wages"),
        )
        for i, m in enumerate((1, 2, 3, 4))
    ]
    analysis = analyse(ledger(*rows))
    assert "ARBEITGEBER" not in named(analysis)


def test_a_merchant_that_refunded_more_than_it_took_is_left_out():
    """
    The ranking answers where the money went.

    A shop with a net credit for the year has no place on it. The refund still
    nets off the category total, which is where that arithmetic belongs.
    """
    rows = [
        payment("buy", 2025, 2, 3, -400.0, "ELEKTRONIK"),
        payment("back", 2025, 3, 3, 500.0, "ELEKTRONIK"),
    ]
    analysis = analyse(ledger(*rows))
    assert "ELEKTRONIK" not in named(analysis)


def test_spending_with_no_merchant_name_is_reported_rather_than_spread():
    """
    A booking line holding no merchant still spent money.

    Dividing it among the merchants that could be named, or dropping it from
    the total, would both report a ledger more concentrated than the one in
    the file — the one error this page must not make.
    """
    blanks = [payment(f"blank-{i}", 2025, 4, 4, -100.0, "  ") for i in range(5)]
    analysis = analyse(ledger(*blanks))
    assert analysis.unnamed == pytest.approx(500.0)
    assert sum(merchant.share for merchant in analysis.merchants) < 1.0


def test_the_trailing_partial_month_is_not_ranked():
    """
    Statements land in batches, so the newest month is a few days long.

    Whoever happened to be paid in those days would otherwise be ranked
    against merchants measured over a whole one.
    """
    rows = ledger() + [payment("new", 2025, 7, 2, -2000.0, "MOEBELHAUS")]
    analysis = analyse(rows)
    assert "2025-07" not in analysis.months
    assert "MOEBELHAUS" not in named(analysis)


# --- How few of them there are ------------------------------------------------


def test_the_ranks_say_how_few_merchants_hold_the_spending():
    """
    The two numbers that summarise the curve.

    A ledger where one merchant is four fifths of the spending should reach
    both thresholds at the first rank; that is the whole claim the figure
    above the ranking makes.
    """
    analysis = analyse(
        ledger(merchants=(("EDEKA", -100.0), ("REWE", -1.0), ("ALDI", -1.0)))
    )
    assert analysis.half == 1
    assert analysis.four_fifths == 1
    assert analysis.count == 3


def test_the_curve_never_reaching_a_threshold_reports_nothing():
    """
    Unnamed spending can put a threshold out of reach.

    Returning a rank anyway would quote a number the ledger does not contain.
    """
    blanks = [
        payment(f"blank-{i}", 2025, m, 4, -4000.0, "")
        for i, m in enumerate((1, 2, 3, 4))
    ]
    analysis = analyse(ledger(*blanks))
    assert analysis.four_fifths is None


def test_the_leaderboard_share_covers_the_top_ten():
    analysis = analyse(ledger())
    assert analysis.leaderboard_share == pytest.approx(1.0, abs=0.001)


# --- Inside a category ---------------------------------------------------------


def test_a_category_reports_the_share_its_largest_merchant_holds():
    rows = [
        payment(f"bahn-{i}", 2025, 3, 8, -50.0, "DB", category=TRANSPORT)
        for i in range(4)
    ]
    rows += [
        payment(f"hvv-{i}", 2025, 3, 9, -10.0, "HVV", category=TRANSPORT)
        for i in range(2)
    ]
    rows += [
        payment(f"taxi-{i}", 2025, 3, 10, -5.0, "TAXI", category=TRANSPORT)
        for i in range(2)
    ]
    analysis = analyse(ledger(*rows))
    transit = next(c for c in analysis.categories if c.category == "Public Transit")
    assert transit.merchants == 3
    assert transit.leader == "DB"
    assert transit.leader_share == pytest.approx(200 / 230, abs=0.001)


def test_a_category_with_too_few_merchants_is_not_called_concentrated():
    """
    One merchant in a category is 100% of it by definition.

    That is a fact about the taxonomy rather than about the spending, and a
    row saying so would push the categories worth reading off the list.
    """
    rows = [
        payment(f"bahn-{i}", 2025, 3, 8, -50.0, "DB", category=TRANSPORT)
        for i in range(4)
    ]
    analysis = analyse(ledger(*rows))
    assert all(c.category != "Public Transit" for c in analysis.categories)


# --- The window before the window ----------------------------------------------


def test_the_previous_window_is_compared_when_the_history_holds_one():
    analysis = analyse(ledger(months=30))
    assert analysis.previous
    assert analysis.merchants[0].previous_total is not None


def test_a_short_history_reports_no_comparison_rather_than_a_zero():
    """
    A missing window is not a window in which nothing was spent.

    Reporting zero would show every merchant as newly arrived and every ticket
    as having appeared out of nothing.
    """
    analysis = analyse(ledger(months=14))
    assert analysis.previous == ()
    assert analysis.merchants[0].previous_total is None


# --- Nothing to rank -----------------------------------------------------------


def test_an_empty_ledger_produces_an_empty_ranking():
    analysis = analyse([])
    assert analysis.merchants == ()
    assert analysis.count == 0
