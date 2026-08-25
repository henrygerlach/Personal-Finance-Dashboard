"""
The classification prompt.

Kept out of the request code on purpose: the prompt is the part that will be
edited most often, and every edit invalidates the cached prefix, so it should
be visible and reviewable on its own rather than buried in a handler.

Everything in ``build_system_prompt`` is constant across requests — the
instructions, the hierarchy and the examples. That block is sent with a cache
breakpoint, so a batch pays for it once and reads it at a tenth of the price
afterwards. Anything that varies per request goes in the user message, after
the breakpoint. Adding a timestamp, a transaction count or a merchant name to
the system block would silently defeat the whole arrangement.
"""

from app.categories import CATEGORY_HIERARCHY, iter_pairs

INSTRUCTIONS = """\
You classify German bank transactions into a fixed two-level category system.

Each transaction gets exactly one major category and one minor category from \
the hierarchy below. Copy both names verbatim. A pair that is not in the \
hierarchy, or a minor taken from a different major, is discarded and the \
transaction is sent for human review — so an accurate "I am unsure" is worth \
more than a confident guess.

How to read the input:

- `text` is the booking line reduced to the merchant. Payment-processor \
prefixes, card and reference numbers and dates have already been removed.
- `amount` is in EUR. Negative is money out, positive is money in. Use it as \
evidence — a positive amount at a supermarket is a refund, not groceries — \
but never as the sole basis for a category.
- `process` is the bank's own transaction type (Lastschrift, Visa-Umsatz, \
Kartenumsatz, Übertrag, Wertpapiere and so on).

Rules that override intuition:

- Do not decide whether something is income, an expense or a transfer. That \
follows from the major category and is derived afterwards.
- Money moved between the owner's own accounts, securities purchases, cash \
withdrawals and credit card settlements belong under Transfers & Savings, \
even though money leaves the account.
- Maintenance, allowances or support *received* is income, not an expense. \
The German word "Unterhalt" on an incoming payment means received support.
- Bank charges, card fees and foreign-transaction surcharges are Financial, \
never the category of whatever was bought.
- A person's name as the merchant usually means a private transfer, unless \
the amount and rhythm clearly say otherwise.

`confidence` is your probability that the pair is right, from 0 to 1. Use the \
full range. Below the configured threshold the transaction goes to review \
instead of being filed, which is the correct outcome when the text is \
genuinely ambiguous.

`reason` is one short sentence naming the evidence you used.\
"""

#: Cases the rule system got wrong or needed a hand-written rule for, taken
#: from the live statement data. Each one teaches a distinction the
#: instructions state but that is easy to lose on a short merchant string.
FEW_SHOT_EXAMPLES = [
    {
        "text": "ACADEMY FAHRSCHULE HOHELUFTGBR MERSCH UND KOLLEGEN",
        "amount": -295.93,
        "process": "Lastschrift / Belastung",
        "major": "Family & Education",
        "minor": "Tuition & Courses",
        "confidence": 0.93,
        "reason": (
            "A driving school charges for lessons, which is tuition, not "
            "vehicle running costs."
        ),
    },
    {
        "text": "FRANZ MARTIN SUNTRUP UNTERHALT",
        "amount": 250.0,
        "process": "Übertrag / Überweisung",
        "major": "Income",
        "minor": "Other Income",
        "confidence": 0.9,
        "reason": (
            "Incoming maintenance payment, so it is income rather than a "
            "support expense."
        ),
    },
    {
        "text": "1,75 PROZ.AUSLANDSENTGELT",
        "amount": -0.24,
        "process": "Lastschrift / Belastung",
        "major": "Financial",
        "minor": "Bank & Card Fees",
        "confidence": 0.97,
        "reason": (
            "A foreign-transaction surcharge is a card fee, not part of the "
            "purchase it accompanies."
        ),
    },
    {
        "text": "ENTGELT VISA-KREDITKARTE",
        "amount": -1.9,
        "process": "Kontoführungsentgelt",
        "major": "Financial",
        "minor": "Bank & Card Fees",
        "confidence": 0.97,
        "reason": "A recurring card charge billed by the bank itself.",
    },
    {
        "text": "X(IE)-MSCI WORLD 1C WPKNR: A1XB5U ISIN: IE00BJ0KDQ92",
        "amount": -250.0,
        "process": "Wertpapiere",
        "major": "Transfers & Savings",
        "minor": "Investments",
        "confidence": 0.96,
        "reason": (
            "A securities purchase moves money into an asset rather than spending it."
        ),
    },
    {
        "text": "MENSA HARBURG",
        "amount": -3.6,
        "process": "Visa-Umsatz",
        "major": "Food & Drink",
        "minor": "Cafés & Snacks",
        "confidence": 0.85,
        "reason": "A university canteen at a small amount is a cheap prepared meal.",
    },
    {
        "text": "STUDIERENDENWERK HAMBURG",
        "amount": -413.0,
        "process": "Übertrag / Überweisung",
        "major": "Housing",
        "minor": "Rent",
        "confidence": 0.82,
        "reason": (
            "A monthly four-figure payment to a student services body is "
            "hall-of-residence rent."
        ),
    },
    {
        "text": "SPOTIFY AB",
        "amount": -4.99,
        "process": "Lastschrift / Belastung",
        "major": "Leisure & Entertainment",
        "minor": "Streaming & Media",
        "confidence": 0.96,
        "reason": "A music streaming subscription.",
    },
    {
        "text": "WWW.STEAMPOWERED.COM",
        "amount": -19.99,
        "process": "Lastschrift / Belastung",
        "major": "Leisure & Entertainment",
        "minor": "Games",
        "confidence": 0.94,
        "reason": "Steam sells computer games.",
    },
    {
        "text": "MAREN GERLACH WEIHNACHTSGELD VON MAMA OMA OPA",
        "amount": 125.0,
        "process": "Übertrag / Überweisung",
        "major": "Gifts & Donations",
        "minor": "Gifts",
        "confidence": 0.78,
        "reason": "An incoming private transfer described as a Christmas present.",
    },
    {
        "text": "EDEKA BOECKER",
        "amount": 12.4,
        "process": "Lastschrift / Belastung",
        "major": "Income",
        "minor": "Refunds & Reimbursements",
        "confidence": 0.72,
        "reason": (
            "A positive amount at a supermarket is a refund, not a grocery purchase."
        ),
    },
    {
        "text": "OTTO PAYMENTS",
        "amount": -89.24,
        "process": "Übertrag / Überweisung",
        "major": "Housing",
        "minor": "Furniture & Household Goods",
        "confidence": 0.75,
        "reason": (
            "Furniture and household goods sit under Housing, not under Shopping."
        ),
    },
    {
        "text": "COMMERZBANK ATM",
        "amount": -100.0,
        "process": "Bargeldauszahlung",
        "major": "Transfers & Savings",
        "minor": "Cash Withdrawal",
        "confidence": 0.98,
        "reason": "Cash leaving the account is not yet spending.",
    },
]


def render_hierarchy() -> str:
    """The full hierarchy as an indented list, in its canonical order."""
    lines = []
    for major, minors in CATEGORY_HIERARCHY.items():
        lines.append(f"{major}")
        lines.extend(f"  - {minor}" for minor in minors)
    return "\n".join(lines)


def render_examples() -> str:
    """The few-shot examples as input/output pairs."""
    blocks = []
    for example in FEW_SHOT_EXAMPLES:
        blocks.append(
            f"text: {example['text']}\n"
            f"amount: {example['amount']}\n"
            f"process: {example['process']}\n"
            f"-> {example['major']} / {example['minor']} "
            f"(confidence {example['confidence']}) — {example['reason']}"
        )
    return "\n\n".join(blocks)


def build_system_prompt() -> str:
    """
    The constant part of every request.

    Must not contain anything that varies between requests, or the prompt
    cache never gets a hit.
    """
    return (
        f"{INSTRUCTIONS}\n\n"
        f"# Category hierarchy\n\n{render_hierarchy()}\n\n"
        f"# Worked examples\n\n{render_examples()}\n"
    )


def build_response_schema() -> dict:
    """
    The JSON schema the response is constrained to.

    Majors and minors are enumerated, so the model cannot invent a name. It
    can still pair a valid minor with the wrong major, which the schema has no
    way to express — ``app.categories.coerce_pair`` catches that server-side.
    """
    majors = sorted(CATEGORY_HIERARCHY)
    minors = sorted({minor for _, minor in iter_pairs()})
    return {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "The id of the transaction being classified",
                        },
                        "major": {"type": "string", "enum": majors},
                        "minor": {"type": "string", "enum": minors},
                        # The API rejects minimum/maximum on a number, so the
                        # range is stated in the instructions and enforced by
                        # the client when the response is parsed.
                        "confidence": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "major", "minor", "confidence", "reason"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    }
