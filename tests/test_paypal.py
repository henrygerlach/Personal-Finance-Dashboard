"""
Reading PayPal exports and matching them to bank lines.

The tests are grouped the way the failures are: reading a file in either
locale, deciding which rows the bank ever saw and who stood behind them, and
refusing to answer when two settlements fit equally well.
"""

from datetime import date

import pytest

from app.paypal.ledger import PayPalLedger
from app.paypal.records import (
    PayPalFormatError,
    PayPalRow,
    looks_like_export,
    parse_amount,
    parse_export,
)

# --- Fixtures ----------------------------------------------------------------

GERMAN_HEADER = (
    '"Datum","Uhrzeit","Zeitzone","Name","Typ","Status","Währung","Brutto",'
    '"Gebühr","Netto","Transaktionscode","Artikelbezeichnung",'
    '"Zugehöriger Transaktionscode"'
)

ENGLISH_HEADER = (
    '"Date","Time","Time zone","Name","Type","Status","Currency","Gross",'
    '"Fee","Net","Transaction ID","Item Title","Reference Txn ID"'
)


def german_row(
    datum: str,
    name: str,
    typ: str,
    brutto: str,
    code: str,
    related: str = "",
    title: str = "",
) -> str:
    return (
        f'"{datum}","12:00:00","CET","{name}","{typ}","Abgeschlossen","EUR",'
        f'"{brutto}","0,00","{brutto}","{code}","{title}","{related}"'
    )


def row(
    code: str,
    day: int,
    kind: str,
    gross: float,
    name: str = "",
    related: str = "",
    month: int = 3,
) -> PayPalRow:
    """A record built directly, for ledger tests that do not need a file."""
    return PayPalRow(
        code=code,
        date=date(2024, month, day),
        kind=kind,
        status="Abgeschlossen",
        name=name,
        currency="EUR",
        gross=gross,
        related_code=related,
        item_title="",
    )


PURCHASE = "PayPal Express-Zahlung"
FUNDING = "Bankgutschrift auf PayPal-Konto"
WITHDRAWAL = "Von Nutzer eingeleitete Abbuchung"
REFUND = "Rückzahlung"
BUYER_CREDIT = "Zahlung per PayPal-Käuferkredit"
GENERAL_PAYMENT = "Allgemeine Zahlung"
PAYPAL_ITSELF = "PayPal (Europe) S.a r.l. et Cie, S.C.A."


def ledger_with(*records: PayPalRow, **options) -> PayPalLedger:
    return PayPalLedger(records, **options)


# --- Amounts -----------------------------------------------------------------


class TestParseAmount:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("-14,69", -14.69),
            ("1.925,36", 1925.36),  # German thousands
            ("1,234.56", 1234.56),  # English thousands
            ("0,00", 0.0),
            ("636,24", 636.24),
            ("1.234", 1234.0),  # three digits after: grouping, not decimals
            ("12", 12.0),
            ("-1.234.567,89", -1234567.89),
        ],
    )
    def test_reads_both_locales(self, raw, expected):
        assert parse_amount(raw) == pytest.approx(expected)

    def test_empty_is_none(self):
        assert parse_amount("") is None
        assert parse_amount(None) is None
        assert parse_amount("   ") is None

    def test_rejects_text(self):
        with pytest.raises(PayPalFormatError):
            parse_amount("about ten euros")


# --- Files -------------------------------------------------------------------


class TestParseExport:
    def test_reads_a_german_export(self):
        payload = "\n".join(
            [
                GERMAN_HEADER,
                german_row("14.09.2025", "DB Vertrieb GmbH", PURCHASE, "-13,49", "AAA"),
                german_row("14.09.2025", "", FUNDING, "13,49", "BBB", related="AAA"),
            ]
        )
        records = parse_export(payload)

        assert [r.code for r in records] == ["AAA", "BBB"]
        assert records[0].date == date(2025, 9, 14)
        assert records[0].name == "DB Vertrieb GmbH"
        assert records[0].gross == pytest.approx(-13.49)
        assert records[1].related_code == "AAA"

    def test_reads_an_english_export(self):
        payload = "\n".join(
            [
                ENGLISH_HEADER,
                '"09/14/2025","12:00:00","CET","Spotify AB","General Payment",'
                '"Completed","EUR","-5.99","0.00","-5.99","AAA","",""',
            ]
        )
        records = parse_export(payload)

        assert records[0].name == "Spotify AB"
        assert records[0].gross == pytest.approx(-5.99)
        # 14 cannot be a month, so the file is month-first.
        assert records[0].date == date(2025, 9, 14)

    def test_utf8_bom_is_stripped(self):
        payload = (
            "﻿"
            + GERMAN_HEADER
            + "\n"
            + german_row("01.02.2024", "Shop", PURCHASE, "-1,00", "AAA")
        ).encode("utf-8")
        assert parse_export(payload)[0].code == "AAA"

    def test_date_order_is_decided_per_file_not_per_row(self):
        """
        01.02 and 03.02 are both readable either way; 25.02 is not.

        One unambiguous row settles the order for the whole file, which is
        what keeps the other two in February rather than in January and March.
        """
        payload = "\n".join(
            [
                GERMAN_HEADER,
                german_row("01.02.2024", "A", PURCHASE, "-1,00", "AAA"),
                german_row("03.02.2024", "B", PURCHASE, "-2,00", "BBB"),
                german_row("25.02.2024", "C", PURCHASE, "-3,00", "CCC"),
            ]
        )
        assert [r.date for r in parse_export(payload)] == [
            date(2024, 2, 1),
            date(2024, 2, 3),
            date(2024, 2, 25),
        ]

    def test_rejects_a_file_that_is_not_an_export(self):
        payload = '"Buchungstag";"Vorgang";"Umsatz in EUR"\n"01.02.2024";"x";"-1,00"'
        with pytest.raises(PayPalFormatError, match="missing column"):
            parse_export(payload)

    def test_rows_without_a_transaction_code_are_skipped(self):
        payload = "\n".join(
            [
                GERMAN_HEADER,
                german_row("01.02.2024", "A", PURCHASE, "-1,00", ""),
                german_row("01.02.2024", "B", PURCHASE, "-2,00", "BBB"),
            ]
        )
        assert [r.code for r in parse_export(payload)] == ["BBB"]


class TestLooksLikeExport:
    """The upload page takes both file types through one control."""

    def test_recognises_an_export(self):
        payload = (
            GERMAN_HEADER
            + "\n"
            + german_row("01.02.2024", "Shop", PURCHASE, "-1,00", "AAA")
        )
        assert looks_like_export(payload) is True

    def test_recognises_an_english_export(self):
        assert looks_like_export(ENGLISH_HEADER) is True

    def test_rejects_a_comdirect_statement(self):
        payload = (
            '";"\n"Umsätze Girokonto";"Zeitraum: 01.10.2024 - 31.10.2024"\n\n'
            '"Buchungstag";"Wertstellung (Valuta)";"Vorgang";"Buchungstext";'
            '"Umsatz in EUR";\n"29.10.2024";"29.10.2024";"Lastschrift";"x";"-3,76";'
        )
        assert looks_like_export(payload) is False

    def test_rejects_nonsense(self):
        assert looks_like_export(b"\x00\x01\x02 not a csv") is False
        assert looks_like_export(b"") is False
        assert looks_like_export("") is False


# --- The ledger --------------------------------------------------------------


class TestSettlements:
    def test_only_bank_facing_rows_become_settlements(self):
        ledger = ledger_with(
            row("AAA", 4, PURCHASE, -13.49, name="DB Vertrieb GmbH"),
            row("BBB", 4, FUNDING, 13.49, related="AAA"),
        )
        assert [s.code for s in ledger.settlements] == ["BBB"]

    def test_funding_debits_the_bank_and_withdrawal_credits_it(self):
        ledger = ledger_with(
            row("AAA", 4, PURCHASE, -10.0, name="Shop"),
            row("BBB", 4, FUNDING, 10.0, related="AAA"),
            row("CCC", 6, WITHDRAWAL, -20.0, related="DDD"),
            row("DDD", 6, REFUND, 20.0, name="Shop"),
        )
        directions = {s.code: s.direction for s in ledger.settlements}
        assert directions == {"BBB": -1, "CCC": 1}

    def test_payee_is_followed_through_two_hops(self):
        """A refund reaches its merchant only via the refund row."""
        ledger = ledger_with(
            row("CCC", 6, WITHDRAWAL, -20.0, related="DDD"),
            row("DDD", 6, REFUND, 20.0, name="ASOS.com Limited"),
        )
        assert ledger.settlements[0].counterparty == "ASOS.com Limited"

    def test_a_settlement_with_no_payee_keeps_an_empty_one(self):
        ledger = ledger_with(row("CCC", 6, WITHDRAWAL, -900.0))
        assert ledger.settlements[0].counterparty == ""

    def test_a_pointer_cycle_terminates(self):
        ledger = ledger_with(
            row("AAA", 4, FUNDING, 10.0, related="BBB"),
            row("BBB", 4, FUNDING, 10.0, related="AAA"),
        )
        assert all(s.counterparty == "" for s in ledger.settlements)

    def test_foreign_currency_settlements_are_ignored(self):
        record = row("AAA", 4, FUNDING, 10.0, related="BBB")
        foreign = PayPalRow(**{**record.__dict__, "currency": "USD"})
        assert ledger_with(foreign).settlements == ()


class TestPayPalIsNeverThePayee:
    """
    PayPal stands between the two parties and is neither of them.

    Accepting it re-introduces exactly what app.normalization exists to strip
    out of a booking text, and gives the classifier a name it cannot use.
    """

    def test_a_chain_ending_at_paypal_is_not_an_answer(self):
        ledger = ledger_with(
            row("PAYTOPP", 4, GENERAL_PAYMENT, -50.0, name=PAYPAL_ITSELF),
            row("FUND1", 4, FUNDING, 50.0, related="PAYTOPP"),
        )
        assert ledger.settlements[0].counterparty == ""
        assert ledger.resolve(-50.0, date(2024, 3, 5)) is None

    def test_a_shop_that_merely_mentions_paypal_is_still_a_payee(self):
        ledger = ledger_with(
            row("PAY1", 4, PURCHASE, -50.0, name="Paypalace Hotels"),
            row("FUND1", 4, FUNDING, 50.0, related="PAY1"),
        )
        assert ledger.settlements[0].counterparty == "Paypalace Hotels"


class TestDeferredPayment:
    """
    "Pay in 30 days": PayPal pays the shop now and collects from the bank
    later, so the bank line the user sees is a month adrift from the purchase
    and names PayPal. This is the case reported against the bike purchase of
    5 March 2025.
    """

    def build(self, **options):
        return ledger_with(
            # 2 February: bought on credit. Nothing reaches the bank.
            row("PAY1", 2, PURCHASE, -1522.99, name="H&S Bike-Discount GmbH", month=2),
            row("CREDIT", 2, BUYER_CREDIT, 1522.99, related="PAY1", month=2),
            # 4 March: PayPal collects, naming itself and pointing nowhere.
            row("REPAY", 4, GENERAL_PAYMENT, -1522.99, name=PAYPAL_ITSELF),
            row("FUND1", 4, FUNDING, 1522.99, related="REPAY"),
            **options,
        )

    def test_the_shop_is_recovered_through_the_advance(self):
        match = self.build().resolve(-1522.99, date(2024, 3, 5))
        assert match is not None
        assert match.counterparty == "H&S Bike-Discount GmbH"

    def test_the_weaker_evidence_is_recorded(self):
        match = self.build().resolve(-1522.99, date(2024, 3, 5))
        assert match.settlement.linked_by_amount is True
        assert match.settlement.source_code == "PAY1"

    def test_a_pointer_match_is_not_flagged(self):
        ledger = ledger_with(
            row("PAY1", 4, PURCHASE, -10.0, name="Spotify AB"),
            row("FUND1", 4, FUNDING, 10.0, related="PAY1"),
        )
        assert ledger.settlements[0].linked_by_amount is False

    def test_the_lag_is_measured_against_the_collection_not_the_purchase(self):
        """
        The bank booked one day after PayPal collected, not thirty-one days
        after the bike was bought — the collection is what it settles.
        """
        match = self.build().resolve(-1522.99, date(2024, 3, 5))
        assert match.lag_days == 1

    def test_an_advance_older_than_the_window_is_not_reached_for(self):
        ledger = ledger_with(
            row("PAY1", 2, PURCHASE, -1522.99, name="H&S Bike-Discount", month=1),
            row("CREDIT", 2, BUYER_CREDIT, 1522.99, related="PAY1", month=1),
            row("REPAY", 4, GENERAL_PAYMENT, -1522.99, name=PAYPAL_ITSELF, month=9),
            row("FUND1", 4, FUNDING, 1522.99, related="REPAY", month=9),
        )
        assert ledger.resolve(-1522.99, date(2024, 9, 5)) is None

    def test_two_advances_naming_different_shops_resolve_to_nothing(self):
        ledger = ledger_with(
            row("PAY1", 2, PURCHASE, -100.0, name="Shop One", month=2),
            row("CREDIT1", 2, BUYER_CREDIT, 100.0, related="PAY1", month=2),
            row("PAY2", 3, PURCHASE, -100.0, name="Shop Two", month=2),
            row("CREDIT2", 3, BUYER_CREDIT, 100.0, related="PAY2", month=2),
            row("REPAY", 4, GENERAL_PAYMENT, -100.0, name=PAYPAL_ITSELF),
            row("FUND1", 4, FUNDING, 100.0, related="REPAY"),
        )
        assert ledger.resolve(-100.0, date(2024, 3, 5)) is None

    def test_an_unlinked_refund_withdrawn_to_the_bank_finds_its_shop(self):
        """
        The same missing-pointer shape: a shop refunds into the balance and
        the balance is withdrawn, with nothing joining the two rows.
        """
        ledger = ledger_with(
            row("REF", 4, REFUND, 29.99, name="H&M Hennes & Mauritz"),
            row("OUT", 4, "Allgemeine Abbuchung", -29.99),
        )
        match = ledger.resolve(29.99, date(2024, 3, 5))
        assert match is not None
        assert match.counterparty == "H&M Hennes & Mauritz"
        assert match.settlement.linked_by_amount is True

    def test_a_bare_balance_withdrawal_still_resolves_to_nothing(self):
        """Money moved to one's own account was not paid to anybody."""
        ledger = ledger_with(row("OUT", 4, WITHDRAWAL, -901.0))
        assert ledger.resolve(901.0, date(2024, 3, 5)) is None


class TestResolve:
    def build(self, **options) -> PayPalLedger:
        return ledger_with(
            row("PAY1", 4, PURCHASE, -13.49, name="DB Vertrieb GmbH"),
            row("FUND1", 4, FUNDING, 13.49, related="PAY1"),
            **options,
        )

    def test_matches_a_debit_booked_two_days_later(self):
        match = self.build().resolve(-13.49, date(2024, 3, 6))
        assert match is not None
        assert match.counterparty == "DB Vertrieb GmbH"
        assert match.lag_days == 2
        assert match.settlement.code == "FUND1"

    def test_does_not_match_a_booking_before_the_settlement(self):
        """The bank never books a PayPal pull before PayPal makes it."""
        assert self.build().resolve(-13.49, date(2024, 3, 3)) is None

    def test_does_not_match_beyond_the_window(self):
        assert self.build().resolve(-13.49, date(2024, 3, 20)) is None

    def test_window_is_configurable(self):
        ledger = self.build(max_lag_days=30)
        assert ledger.resolve(-13.49, date(2024, 3, 20)) is not None

    def test_a_different_amount_does_not_match(self):
        assert self.build().resolve(-13.50, date(2024, 3, 6)) is None

    def test_direction_must_agree(self):
        """A credit of the same size is not the debit that funded a purchase."""
        assert self.build().resolve(13.49, date(2024, 3, 6)) is None

    def test_a_tie_between_two_payees_resolves_to_nothing(self):
        ledger = ledger_with(
            row("PAY1", 4, PURCHASE, -10.0, name="Antonia Viebrock"),
            row("FUND1", 4, FUNDING, 10.0, related="PAY1"),
            row("PAY2", 5, PURCHASE, -10.0, name="Palak Pandya"),
            row("FUND2", 5, FUNDING, 10.0, related="PAY2"),
        )
        assert ledger.candidates(-10.0, date(2024, 3, 6)) != []
        assert ledger.resolve(-10.0, date(2024, 3, 6)) is None

    def test_two_settlements_naming_the_same_payee_still_resolve(self):
        """Paying one shop twice is not an ambiguity about who was paid."""
        ledger = ledger_with(
            row("PAY1", 4, PURCHASE, -10.0, name="Spotify AB"),
            row("FUND1", 4, FUNDING, 10.0, related="PAY1"),
            row("PAY2", 5, PURCHASE, -10.0, name="Spotify AB"),
            row("FUND2", 5, FUNDING, 10.0, related="PAY2"),
        )
        match = ledger.resolve(-10.0, date(2024, 3, 6))
        assert match is not None and match.counterparty == "Spotify AB"

    def test_a_settlement_with_no_payee_is_not_a_match(self):
        ledger = ledger_with(row("CCC", 4, FUNDING, 50.0))
        assert ledger.resolve(-50.0, date(2024, 3, 6)) is None

    def test_nearest_settlement_wins_among_equals(self):
        ledger = ledger_with(
            row("PAY1", 1, PURCHASE, -10.0, name="Spotify AB"),
            row("FUND1", 1, FUNDING, 10.0, related="PAY1"),
            row("PAY2", 5, PURCHASE, -10.0, name="Spotify AB"),
            row("FUND2", 5, FUNDING, 10.0, related="PAY2"),
        )
        match = ledger.resolve(-10.0, date(2024, 3, 6))
        assert match is not None and match.settlement.code == "FUND2"

    def test_an_empty_ledger_answers_nothing(self):
        assert PayPalLedger([]).resolve(-10.0, date(2024, 3, 6)) is None
