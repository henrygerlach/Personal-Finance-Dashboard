"""
Turning a Comdirect booking line into a stable merchant key.

Comdirect packs a lot into ``booking_text``: field labels, the payment
processor's own name, terminal and customer reference numbers, card numbers,
booking dates and a trailing reference. None of that identifies the merchant,
but all of it varies per transaction — so using the raw text as a cache key
would mean every visit to the same shop looks like a new merchant.

``normalize_merchant`` strips the machinery and returns what is left, upper
case and whitespace-collapsed. The result is the lookup key for the merchant
cache and the text the classifier is shown.

Two cases deserve their own note:

*Payment processors.* A PayPal debit names PayPal as the ordering party and
hides the real merchant inside the booking text. The processor prefix is
stripped and the merchant behind it is used instead, so "Spotify" is cached
as Spotify and not as PayPal.

*PayPal without a merchant.* Comdirect sometimes truncates the merchant away
entirely, leaving ``PP.5189.PP . , Ihr Ei nkauf bei Ref. ...``. There is no
merchant in that string — not for a rule, not for a model. Those normalise to
the empty string, and ``is_unresolvable_paypal`` identifies them so the caller
can apply its fallback instead of asking the API a question with no answer.
"""

import re

# --- Payment processors ------------------------------------------------------

#: Ordering parties that are payment processors rather than merchants. The
#: real merchant, when present, sits inside the booking text.
_PAYPAL_PATTERN = re.compile(r"\bPayPal\b|\bPP\.\d+\.PP\b", re.IGNORECASE)

#: The merchant in a PayPal line sits between the last dot-space separator and
#: "Ihr Einkauf bei". Comdirect wraps that phrase mid-word, so "Einkauf" may
#: arrive split.
#:
#: The separator must be a dot followed by whitespace. A dot inside the name
#: itself never is ("ASOS.com"), which is what keeps domain merchants intact —
#: excluding dots from the capture would truncate every one of them to its
#: top-level domain and file ASOS and Steam under the same key. The leading
#: ``.*`` is greedy so the *last* separator wins; otherwise the match would
#: start inside PayPal's own "S.a r.l." and swallow the field label.
_PAYPAL_MERCHANT = re.compile(
    r".*\.\s+([^,]*?)\s*,\s*Ihr\s+Ei\s*nkauf\s+bei",
    re.IGNORECASE,
)

# --- Structural noise --------------------------------------------------------

# The ordering party is the cleanest merchant name Comdirect gives us: the
# booking text below it repeats the name and then appends the terminal noise.
# "Empfänger" also arrives mis-decoded as "EmpfÜnger" — the CSV is ISO-8859-1
# and some exports are already mangled at the source, so the umlaut is matched
# loosely. The value ends at the next field label, which is not always
# preceded by a space ("ToshmatovaKto/IBAN:").
_ORDERING_PARTY = re.compile(
    r"(?:Auftraggeber|Empf\S{0,3}nger)\s*:\s*(.*?)"
    r"(?=\s*(?:Buchungstext\s*:|Kto/IBAN\s*:|BLZ/BIC\s*:|$))",
    re.IGNORECASE | re.DOTALL,
)

#: Field labels, for lines that carry no usable ordering party.
_FIELD_LABELS = re.compile(
    r"(?:Auftraggeber|Empf\S{0,3}nger|Buchungstext|Verwendungszweck)\s*:",
    re.IGNORECASE,
)

#: Everything from the trailing reference onwards carries no merchant information.
_REFERENCE_TAIL = re.compile(r"\bRef\.\s*\S*", re.IGNORECASE)

_NOISE_PATTERNS = [
    # Bank identifiers
    re.compile(r"Kto/IBAN\s*:\s*[A-Z]{2}\d{2}[A-Z0-9]*", re.IGNORECASE),
    re.compile(r"BLZ/BIC\s*:\s*[A-Z0-9]+", re.IGNORECASE),
    re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b"),  # bare IBAN
    # Card terminal and customer reference numbers
    re.compile(r"\bKDN-REF\s*\d+", re.IGNORECASE),
    re.compile(r"\bKARTE\s*\d+", re.IGNORECASE),
    # PayPal plumbing
    re.compile(r"\bPP\.\d+\.PP\b", re.IGNORECASE),
    re.compile(r"\bIhr\s+Ei\s*nkauf\s+bei\b", re.IGNORECASE),
    re.compile(r"\bABBUCH(?:UNG)?\b", re.IGNORECASE),
    # Billing periods: "Zeitraum: 01.12.2023 bis 31.12.2023"
    re.compile(r"\bZeitraum\s*:.*?(?=\bRef\.|$)", re.IGNORECASE | re.DOTALL),
    # Invoice references: "Rechnung 18760 vom 17.05.2024"
    re.compile(r"\bRechnung\s+\d+\s+vom\b", re.IGNORECASE),
    # Dates, in both the dotted and the run-together card format
    re.compile(r"\b\d{2}\.\d{2}\.\d{2,4}\b"),
    re.compile(r"\b\d{8}\b"),
    # Any remaining long digit run is a reference number, not a merchant
    re.compile(r"\b\d{6,}\b"),
    # Visa exports pad the line with a trailing zero group
    re.compile(r"\s0{3}\s*$"),
]

#: Left over once the noise is gone: separators and stray punctuation.
_PUNCTUATION_EDGES = re.compile(r"^[\s.,;:/*+\-]+|[\s.,;:/*+\-]+$")
_WHITESPACE = re.compile(r"\s+")

# German umlauts survive the ISO-8859-1 export inconsistently: the same
# merchant appears as "FÜR", "FUER" and "F R" across statements. Folding to
# the ASCII spelling collapses the first two onto one key.
_UMLAUT_FOLDING = str.maketrans(
    {
        "Ä": "AE",
        "Ö": "OE",
        "Ü": "UE",
        "ä": "AE",
        "ö": "OE",
        "ü": "UE",
        "ß": "SS",
        "É": "E",
        "È": "E",
        "Ê": "E",
        "é": "E",
        "è": "E",
        "ê": "E",
        "Á": "A",
        "À": "A",
        "á": "A",
        "à": "A",
        "Í": "I",
        "í": "I",
        "Ó": "O",
        "ó": "O",
        "Ú": "U",
        "ú": "U",
        "Ñ": "N",
        "ñ": "N",
        "Ç": "C",
        "ç": "C",
    }
)


def is_paypal(booking_text: str) -> bool:
    """Whether the booking line was settled through PayPal."""
    return bool(_PAYPAL_PATTERN.search(booking_text or ""))


def _extract_paypal_merchant(booking_text: str) -> str:
    """The merchant behind a PayPal debit, or an empty string if it was truncated."""
    match = _PAYPAL_MERCHANT.search(booking_text)
    return match.group(1).strip() if match else ""


def _strip_noise(text: str) -> str:
    """Remove field labels, reference numbers, dates and terminal identifiers."""
    text = _FIELD_LABELS.sub(" ", text)
    text = _REFERENCE_TAIL.sub(" ", text)
    for pattern in _NOISE_PATTERNS:
        text = pattern.sub(" ", text)
    return text


def _tidy(text: str) -> str:
    """Fold umlauts, collapse whitespace, trim punctuation and upper-case."""
    text = text.translate(_UMLAUT_FOLDING)
    text = _WHITESPACE.sub(" ", text)
    text = _PUNCTUATION_EDGES.sub("", text)
    return text.strip().upper()


def normalize_merchant(booking_text: str | None, process: str | None = None) -> str:
    """
    Reduce a booking line to a stable merchant key.

    Args:
        booking_text: The raw ``booking_text`` of a transaction.
        process: The Vorgang code. Accepted so callers can pass a whole
            transaction through without special-casing; the current rules do
            not depend on it, and it is ignored.

    Returns:
        The merchant in upper case with whitespace collapsed, or an empty
        string when the line contains no merchant at all.
    """
    if not booking_text:
        return ""

    text = booking_text
    if is_paypal(text):
        # The processor's own name would otherwise swallow every PayPal
        # merchant into one cache entry.
        merchant = _extract_paypal_merchant(text)
        if not merchant:
            return ""
        return _tidy(_strip_noise(merchant))

    # Prefer the ordering party. Cleaning the whole line instead would repeat
    # the merchant, because the booking text below restates it with the
    # terminal and reference numbers appended.
    ordering_party = _ORDERING_PARTY.search(text)
    if ordering_party:
        candidate = _tidy(_strip_noise(ordering_party.group(1)))
        if candidate:
            return candidate

    return _tidy(_strip_noise(text))


def is_unresolvable_paypal(booking_text: str | None) -> bool:
    """
    Whether this is a PayPal line whose merchant Comdirect dropped.

    These carry no signal for any classifier. The caller applies its
    configured fallback category instead of spending an API call on them.
    """
    if not booking_text or not is_paypal(booking_text):
        return False
    return not normalize_merchant(booking_text)
