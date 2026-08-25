"""
Tests for merchant normalization.

The examples are taken verbatim from the live statement data, including its
defects: the mis-decoded umlauts, the word "Einkauf" wrapped mid-word, and
the truncated PayPal lines.
"""

import pytest

from app.normalization import (
    is_paypal,
    is_unresolvable_paypal,
    normalize_merchant,
)

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
