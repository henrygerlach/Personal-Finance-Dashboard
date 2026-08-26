"""
Tests for merchant normalization.

The examples are taken verbatim from the live statement data, including its
defects: the mis-decoded umlauts, the word "Einkauf" wrapped mid-word, and
the truncated PayPal lines.
"""

import re

import pytest

from app.normalization import (
    build_canonical_map,
    is_paypal,
    is_unresolvable_paypal,
    merchant_key,
    normalize_merchant,
    shorten_merchant,
)
from tests.fixtures.booking_texts import CASES, SAME_MERCHANT_PAIRS

# --- Direct debits (Lastschrift) --------------------------------------------


def test_direct_debit_keeps_only_the_ordering_party():
    """
    The booking text restates the merchant and appends terminal noise, so
    normalizing the whole line would return the name twice.
    """
    text = (
        "Auftraggeber: EDEKA M. PETERSEN Buchungstext: EDEKA M. PETERSEN "
        "HAMBURG KARTE 0132 28122022 KDN-REF 000000570661 "
        "Ref. JP22236453035320/128641"
    )
    assert normalize_merchant(text) == "EDEKA M. PETERSEN"


def test_invoice_number_and_date_are_dropped():
    text = (
        "Auftraggeber: ACADEMY FAHRSCHULE HOHELUFTGBR MERSCH UND KOLLEGEN "
        "Buchungstext: Rechnung 18760 vom 17.05.2024"
    )
    assert normalize_merchant(text) == (
        "ACADEMY FAHRSCHULE HOHELUFTGBR MERSCH UND KOLLEGEN"
    )


def test_recipient_label_and_iban_are_dropped():
    """The export writes the next label without a separating space."""
    text = (
        "EmpfÜnger: Lena HoffmannKto/IBAN: DE00000000000000000000 "
        "BLZ/BIC: DEUTDEDBHAM  Buchungstext: Miete"
    )
    assert normalize_merchant(text) == "LENA HOFFMANN"


def test_billing_period_is_dropped():
    text = (
        " Buchungstext: Entgelt Visa-Kreditkarte "
        "Zeitraum: 01.12.2023 bis 31.12.2023 Ref. 6V2C1MIL3"
    )
    assert normalize_merchant(text) == "ENTGELT VISA-KREDITKARTE"


# --- Card payments (Visa-Umsatz, Kartenumsatz) ------------------------------


def test_card_payment_drops_the_trailing_zero_group():
    assert normalize_merchant(" SVAADISH HOHELUFT GMBH HAMBURG 000 ") == (
        "SVAADISH HOHELUFT GMBH HAMBURG"
    )


def test_bare_card_payment_is_returned_as_is():
    assert normalize_merchant(" MEDIA MARKT ") == "MEDIA MARKT"


# --- PayPal ------------------------------------------------------------------


def test_paypal_resolves_to_the_merchant_behind_it():
    """
    The processor's own name must not become the cache key, or every PayPal
    purchase would collapse into a single merchant.
    """
    text = (
        "Auftraggeber: PayPal (Europe) S.a r.l. et Cie, S.C.A. "
        "Buchungstext: 1023434825177 PP.5189.PP . Spotify AB, "
        "Ihr Einkauf bei Ref. XY123/4"
    )
    assert normalize_merchant(text) == "SPOTIFY AB"


def test_paypal_merchant_is_found_when_einkauf_is_wrapped_mid_word():
    text = (
        "PayPal (Europe) S.a r.l. et Cie, S.C.A. Buchungstext: "
        "PP.5189.PP . Zalando SE, Ihr Ei nkauf bei Ref. AB1/2"
    )
    assert normalize_merchant(text) == "ZALANDO SE"


def test_paypal_domain_merchants_keep_their_dots():
    """
    A merchant name may contain dots, and cutting at the first one would file
    every domain under its top-level domain: ASOS and Steam both became "COM"
    and shared a cache entry, and therefore a category.
    """
    asos = (
        "Auftraggeber: PayPal (Europe) S.a r.l. et Cie, S.C.A. Buchungstext: "
        ". ASOS.com, Ihr Einkauf bei ASOS.co m Ref. JK2220957"
    )
    steam = (
        "Auftraggeber: PayPal (Europe) S.a.r.l. et Cie., S.C.A. Buchungstext: "
        "PP.5189.PP . www.steampowered.com, Ihr Einkauf bei "
    )
    assert normalize_merchant(asos) == "ASOS.COM"
    assert normalize_merchant(steam) == "WWW.STEAMPOWERED.COM"
    assert normalize_merchant(asos) != normalize_merchant(steam)


def test_paypal_merchant_is_taken_after_the_last_separator():
    """
    PayPal's own name contains dot-space sequences ("S.a r.l. et Cie"), so the
    merchant must be read from the last separator, not the first — otherwise
    the field label ends up inside the key.
    """
    text = (
        "Auftraggeber: PayPal (Europe) S.a r.l. et Cie, S.C.A. Buchungstext: "
        ". ASOS.com, Ihr Einkauf bei"
    )
    assert "BUCHUNGSTEXT" not in normalize_merchant(text)


def test_paypal_without_a_processor_prefix_still_resolves():
    text = (
        "PayPal (Europe) S.a r.l. et Cie, S.C.A. Buchungstext: "
        ". Nike, Ihr Einkauf bei Nike ABBUCH"
    )
    assert normalize_merchant(text) == "NIKE"


@pytest.mark.parametrize(
    "text",
    [
        # Merchant field present but empty
        "Auftraggeber: PayPal (Europe) S.a r.l. et Cie, S.C.A. Buchungstext: "
        "1024177396132 PP.5189.PP . , Ihr Ei nkauf bei Ref. J822235371402476/7550",
        # No merchant field at all
        "Auftraggeber: PayPal (Europe) S.a r.l. et Cie, S.C.A. Buchungstext: "
        "1021808533116 PP.5189.PP Ref. I92223",
        "Auftraggeber: PayPal (Europe) S.a r.l. et Cie, S.C.A. Buchungstext: "
        "1021256866196 Ref. HT2221997224",
    ],
)
def test_truncated_paypal_normalizes_to_nothing(text):
    """
    Comdirect drops the merchant on some PayPal lines. There is nothing left
    to classify, and the empty key is what tells the caller to stop.
    """
    assert normalize_merchant(text) == ""
    assert is_paypal(text)
    assert is_unresolvable_paypal(text)


def test_resolvable_paypal_is_not_flagged_unresolvable():
    text = (
        "Auftraggeber: PayPal (Europe) S.a r.l. et Cie, S.C.A. Buchungstext: "
        "1023434825177 PP.5189.PP . Spotify AB, Ihr Einkauf bei Ref. XY/1"
    )
    assert is_paypal(text)
    assert not is_unresolvable_paypal(text)


def test_non_paypal_is_never_flagged_unresolvable():
    """An empty key on a non-PayPal line must not trigger the PayPal fallback."""
    assert not is_unresolvable_paypal(" MEDIA MARKT ")
    assert not is_unresolvable_paypal("Ref. ABC123")
    assert not is_paypal(" MEDIA MARKT ")


# --- Umlauts and encoding damage --------------------------------------------


def test_umlauts_are_folded_to_their_ascii_spelling():
    """
    The same merchant arrives spelled both ways across statements, because
    the ISO-8859-1 export is inconsistent.
    """
    assert normalize_merchant("Auftraggeber: Bäckerei Müller Buchungstext: x") == (
        "BAECKEREI MUELLER"
    )
    assert normalize_merchant(
        "Auftraggeber: DIFICO INSTITUT F ÜR FINANZANALYSE Buchungstext: x"
    ) == normalize_merchant(
        "Auftraggeber: DIFICO INSTITUT F UER FINANZANALYSE Buchungstext: x"
    )


def test_sharp_s_is_folded():
    assert normalize_merchant(" Straßenbahn AG 000 ") == "STRASSENBAHN AG"


# --- Stability and edge cases ------------------------------------------------


def test_normalization_is_idempotent():
    """
    The key is written to the cache and looked up again, so normalizing an
    already-normalized value must not change it.
    """
    text = (
        "Auftraggeber: EDEKA BOECKER Buchungstext: EDEKA BOECKER HAMBURG "
        "KARTE 0132 23122022 KDN-REF 000000260698 Ref. HS22236253111512/189198"
    )
    once = normalize_merchant(text)
    assert normalize_merchant(once) == once


def test_the_same_merchant_normalizes_identically_across_statements():
    """Two debits from the same shop must land on one cache entry."""
    first = (
        "Auftraggeber: EDEKA BOECKER Buchungstext: EDEKA BOECKER HAMBURG "
        "KARTE 0132 23122022 KDN-REF 000000260698 Ref. HS2223625/189198"
    )
    second = (
        "Auftraggeber: EDEKA BOECKER Buchungstext: EDEKA BOECKER HAMBURG "
        "KARTE 0132 04012023 KDN-REF 000000981326 Ref. IU2223565/150734"
    )
    assert normalize_merchant(first) == normalize_merchant(second) == "EDEKA BOECKER"


@pytest.mark.parametrize("value", [None, "", "   ", "Ref. ABC/1"])
def test_empty_input_yields_an_empty_key(value):
    assert normalize_merchant(value) == ""


def test_process_argument_is_accepted_and_ignored():
    assert normalize_merchant(" MEDIA MARKT ", "Kartenumsatz") == "MEDIA MARKT"
    assert normalize_merchant(" MEDIA MARKT ", None) == "MEDIA MARKT"


# --- Cache keys --------------------------------------------------------------


def test_merchant_key_removes_spaces():
    assert merchant_key("EDEKA BOECKER") == "EDEKABOECKER"
    assert merchant_key("") == ""


def test_merchant_key_survives_a_wrap_in_the_middle_of_a_word():
    """
    Comdirect chops each field into 35-character pieces and rejoins them with
    a space, which regularly lands mid-word. The same merchant then arrives
    spelled two ways. Undoing the wrap is impossible in general — at the same
    boundary the space is sometimes genuine — but a key without spaces cannot
    be wrong about it.
    """
    assert merchant_key("LOGPAY F INANCIAL SERVICES GMBH") == merchant_key(
        "LOGPAY FINANCIAL SE RVICES GMBH"
    )
    assert merchant_key("WWW.STEA MPOWERED.COM") == merchant_key("WWW.STEAMPOWERED.COM")


def test_merchant_key_still_separates_different_merchants():
    assert merchant_key("ASOS.COM") != merchant_key("WWW.STEAMPOWERED.COM")
    assert merchant_key("EDEKA BOECKER") != merchant_key("EDEKA BOLDT")


def test_shorten_merchant_drops_the_trailing_token():
    assert shorten_merchant("EDEKA BOECKER HAMBURG") == "EDEKA BOECKER"
    assert shorten_merchant("SHOP APOTHEKE B.V") == "SHOP APOTHEKE"


def test_shorten_merchant_keeps_a_minimum_of_two_tokens():
    """
    A one- or two-token name must not be shortened further: a stem like
    "EDEKA" would then absorb every unrelated shop starting with it.
    """
    assert shorten_merchant("MEDIA MARKT") is None
    assert shorten_merchant("SPOTIFY") is None
    assert shorten_merchant("") is None


def test_canonical_map_folds_a_town_suffix_onto_the_shorter_name():
    """
    Card payments append the town, direct debits do not, so one shop produces
    two names. The corpus decides what folds — no list of towns is involved,
    which is what makes this work for statements from anywhere.
    """
    canonical = build_canonical_map(
        ["EDEKA BOECKER", "EDEKA BOECKER HAMBURG", "MENSA HCU", "MENSA HCU HAMBURG"]
    )
    assert canonical["EDEKA BOECKER HAMBURG"] == "EDEKA BOECKER"
    assert canonical["MENSA HCU HAMBURG"] == "MENSA HCU"


def test_canonical_map_leaves_names_with_nothing_to_fold_onto():
    """The shorter spelling must actually occur; a suffix alone proves nothing."""
    canonical = build_canonical_map(["EDEKA BOECKER HAMBURG", "MEDIA MARKT"])
    assert canonical["EDEKA BOECKER HAMBURG"] == "EDEKA BOECKER HAMBURG"
    assert canonical["MEDIA MARKT"] == "MEDIA MARKT"


def test_canonical_map_collapses_a_chain_in_one_pass():
    canonical = build_canonical_map(
        ["REWE PHILIPP MENZ", "REWE PHILIPP MENZ OHG", "REWE PHILIPP MENZ OHG HAMBURG"]
    )
    assert canonical["REWE PHILIPP MENZ OHG HAMBURG"] == "REWE PHILIPP MENZ"


def test_canonical_map_does_not_fold_a_two_token_prefix_away():
    """ "EDEKA" must not absorb "EDEKA BOECKER" and "EDEKA BOLDT"."""
    canonical = build_canonical_map(["EDEKA", "EDEKA BOECKER", "EDEKA BOLDT"])
    assert canonical["EDEKA BOECKER"] == "EDEKA BOECKER"
    assert canonical["EDEKA BOLDT"] == "EDEKA BOLDT"


def test_canonical_map_is_total_and_idempotent():
    names = ["EDEKA BOECKER", "EDEKA BOECKER HAMBURG", "MEDIA MARKT", ""]
    canonical = build_canonical_map(names)
    assert set(canonical) == {n for n in names if n}
    again = build_canonical_map(canonical.values())
    for name, target in canonical.items():
        assert again[target] == target


# --- Against the real statement corpus ---------------------------------------


@pytest.mark.parametrize(
    "booking_text,process,expected,covers",
    [pytest.param(*case, id=case[3][:48]) for case in CASES],
)
def test_real_booking_lines(booking_text, process, expected, covers):
    """
    Every case is a line taken from the live statements, anonymised only where
    a private individual or an IBAN appears.
    """
    assert normalize_merchant(booking_text, process) == expected


@pytest.mark.parametrize(
    "first,second,why",
    [pytest.param(*pair, id=pair[2][:48]) for pair in SAME_MERCHANT_PAIRS],
)
def test_spellings_of_one_merchant_share_a_cache_key(first, second, why):
    """
    These pairs are the whole point of the cache key: if they do not collide,
    the same shop gets two entries, two API calls, and — worse — can end up
    in two different categories.
    """
    left = normalize_merchant(first)
    right = normalize_merchant(second)
    canonical = build_canonical_map([left, right])
    assert merchant_key(canonical[left]) == merchant_key(canonical[right])


def test_the_fixture_covers_every_payment_channel():
    """
    A regression guard on the fixture itself: it is only worth having if it
    still spans the formats the statements actually contain.
    """
    processes = {case[1] for case in CASES if case[1]}
    assert {
        "Lastschrift / Belastung",
        "Visa-Umsatz",
        "Kartenumsatz",
        "Übertrag / Überweisung",
        "Wertpapiere",
        "Barauszahlung",
    } <= processes


def test_the_fixture_contains_no_unanonymised_iban():
    """IBANs in the fixture must be placeholders, not real accounts."""
    for booking_text, *_ in CASES:
        for match in re.findall(r"\bDE\d{2}[0-9A-Z]{16,}", booking_text):
            assert set(match[4:]) <= {"0"}, f"real-looking IBAN in fixture: {match}"
