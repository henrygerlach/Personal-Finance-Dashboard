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


def _wrap_tolerant(phrase: str) -> str:
    """
    A pattern for ``phrase`` that survives Comdirect's line wrapping.

    Fields are chopped into 35-character pieces and rejoined with a space,
    which lands wherever it lands. "Ihr Einkauf bei" occurs in the live data
    as "Ihr Ei nkauf bei", "Ihr Ein kauf bei", "Ihr Einkau f bei", "Ihr E
    inkauf bei", "Ihr Einkauf be i" and more — seven variants of one phrase.
    Matching each by hand is how the first version of this missed 164
    merchants, so the space is allowed between any two characters instead.
    """
    return r"\s*".join(re.escape(char) for char in phrase if not char.isspace())


#: The phrase that marks the end of a PayPal merchant name.
_PURCHASE_MARKER = _wrap_tolerant("Ihr Einkauf bei")

#: The merchant in a PayPal line sits between the last dot-space separator and
#: "Ihr Einkauf bei".
#:
#: The separator must be a dot followed by whitespace. A dot inside the name
#: itself never is ("ASOS.com"), which is what keeps domain merchants intact —
#: excluding dots from the capture would truncate every one of them to its
#: top-level domain and file ASOS and Steam under the same key. The leading
#: ``.*`` is greedy so the *last* separator wins; otherwise the match would
#: start inside PayPal's own "S.a r.l." and swallow the field label.
_PAYPAL_MERCHANT = re.compile(
    rf".*\.\s+([^,]*?)\s*,\s*{_PURCHASE_MARKER}",
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

#: Card-terminal providers that print their own name in front of the shop's.
#: The merchant behind the prefix is the one worth caching — "SumUp Hansa
#: Kebab" is a kebab shop, not a payment company. Stripe and Klarna behave the
#: same way but do not occur in this data, so they are listed without a test.
_PROCESSOR_PREFIX = re.compile(
    r"^\s*(?:SumUp|Stripe|Klarna|iZettle|Zettle)\s+", re.IGNORECASE
)

_NOISE_PATTERNS = [
    # Marketing suffixes some chains append to the booking line.
    re.compile(r"\bSAGT\s+DANKE\b\.?", re.IGNORECASE),
    # Securities identifiers; the fund name in front of them is the merchant.
    re.compile(r"\bWPKNR\s*:\s*\S+", re.IGNORECASE),
    re.compile(r"\bISIN\s*:\s*\S+", re.IGNORECASE),
    # Foreign-currency withdrawals append the local amount and rate.
    # No word boundary: the wrap glues these onto the preceding token
    # ("Saigon Branc690Betrag: -1500000,00 VNDKurs: ...").
    re.compile(r"Betrag\s*:.*$", re.IGNORECASE | re.DOTALL),
    re.compile(r"Kurs\s*:.*$", re.IGNORECASE | re.DOTALL),
    # Bank identifiers
    re.compile(r"Kto/IBAN\s*:\s*[A-Z]{2}\d{2}[A-Z0-9]*", re.IGNORECASE),
    re.compile(r"BLZ/BIC\s*:\s*[A-Z0-9]+", re.IGNORECASE),
    re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b"),  # bare IBAN
    # Card terminal and customer reference numbers
    re.compile(r"\bKDN-REF\s*\d+", re.IGNORECASE),
    re.compile(r"\bKARTE\s*\d+", re.IGNORECASE),
    # PayPal plumbing
    re.compile(r"\bPP\.\d+\.PP\b", re.IGNORECASE),
    re.compile(rf"\b{_PURCHASE_MARKER}\b", re.IGNORECASE),
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

    text = _PROCESSOR_PREFIX.sub("", booking_text)
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


# --- Cache keys --------------------------------------------------------------
#
# The normalized name is what a person and the classifier read. The cache key
# is a separate, deliberately unreadable form of it, because it has a different
# job: it has to make two spellings of one merchant collide.
#
# Comdirect chops each statement field into 35-character pieces and rejoins
# them with a space, which lands mid-word often enough to matter — the same
# shop arrives as "LOGPAY F INANCIAL SERVICES GMBH" and as "LOGPAY FINANCIAL
# SE RVICES GMBH". Undoing that is not possible in general: at the very same
# boundary the space is sometimes genuine, and rejoining those would corrupt
# them. Dropping every space sidesteps the question entirely — the damage
# cannot survive a form that has no spaces to be wrong about.

#: A prefix must keep at least this many tokens to absorb a longer name, so a
#: short stem cannot swallow unrelated merchants that happen to start with it.
_MIN_PREFIX_TOKENS = 2


def merchant_key(normalized: str) -> str:
    """
    The cache key for a normalized merchant name.

    Spaces are removed, which makes the key immune to Comdirect's mid-word
    line wrapping. Two names that differ only by where a wrap fell produce one
    key.
    """
    return (normalized or "").replace(" ", "")


def shorten_merchant(normalized: str) -> str | None:
    """
    Drop the trailing token, or return ``None`` if too little would remain.

    Card payments append the town to the merchant while direct debits do not,
    so the same shop arrives as both "EDEKA BOECKER" and "EDEKA BOECKER
    HAMBURG". A lookup that misses can retry with this to find the entry the
    shorter spelling created.
    """
    tokens = (normalized or "").split()
    if len(tokens) <= _MIN_PREFIX_TOKENS:
        return None
    return " ".join(tokens[:-1])


def build_canonical_map(merchants) -> dict[str, str]:
    """
    Map every merchant name onto the shortest spelling of it in the corpus.

    A name is folded onto another only when dropping its last token yields a
    name that is itself present — the corpus decides, not a list of towns,
    which is what keeps this working for statements from anywhere.

    Args:
        merchants: The normalized names to consider.

    Returns:
        Every input name mapped to its canonical spelling. Names with nothing
        to fold onto map to themselves.
    """
    present = {name for name in merchants if name}
    canonical: dict[str, str] = {}
    # Shortest first, so a fold target is already resolved when it is used and
    # a chain of suffixes collapses in one pass.
    for name in sorted(present, key=lambda value: (len(value.split()), len(value))):
        shorter = shorten_merchant(name)
        if shorter and shorter in present:
            canonical[name] = canonical.get(shorter, shorter)
        else:
            canonical[name] = name
    return canonical
