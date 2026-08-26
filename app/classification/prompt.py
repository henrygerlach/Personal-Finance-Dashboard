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

import json

from app.categories import CATEGORY_HIERARCHY, all_codes, code_for

INSTRUCTIONS = """\
You classify German bank transactions into a fixed two-level category system.

Each transaction gets exactly one category code from the hierarchy below. A \
code names a major/minor pair, so there is no way to combine a minor with the \
wrong major. A code that is not in the list is discarded and the transaction \
is sent for human review — an accurate "I am unsure" is worth more than a \
confident guess.

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
follows from the category and is derived afterwards.
- Money moved between the owner's own accounts, securities purchases, cash \
withdrawals and credit card settlements are transfers, even though money \
leaves the account.
- Maintenance, allowances or support *received* is income, not an expense. \
The German word "Unterhalt" on an incoming payment means received support.
- Bank charges, card fees and foreign-transaction surcharges are Financial, \
never the category of whatever was bought.
- A person's name as the merchant usually means a private transfer, unless \
the amount and rhythm clearly say otherwise.

Answer with one object per input line:

- `id` — the id you were given, unchanged.
- `c` — the category code, copied exactly from the hierarchy.
- `p` — your probability that the code is right, from 0 to 1. Use the full \
range; below {threshold} the transaction goes to review instead of being \
filed, which is the correct outcome when the text is genuinely ambiguous.
- `r` — one short sentence naming the evidence you used. Include it **only** \
when `p` is below {threshold}; those are the cases a person will read. Leave \
it out otherwise.

Return a result for every id you were given, and invent none.\
"""

#: Cases the rule system got wrong or needed a hand-written rule for, taken
#: from the live statement data. Each one teaches a distinction the
#: instructions state but that is easy to lose on a short merchant string.
FEW_SHOT_EXAMPLES = [
    {
        "id": 1,
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
        "id": 2,
        "text": "MARTIN FALKENBERG UNTERHALT",
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
        "id": 3,
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
        "id": 4,
        "text": "ENTGELT VISA-KREDITKARTE",
        "amount": -1.9,
        "process": "Kontoführungsentgelt",
        "major": "Financial",
        "minor": "Bank & Card Fees",
        "confidence": 0.97,
        "reason": "A recurring card charge billed by the bank itself.",
    },
    {
        "id": 5,
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
        "id": 6,
        "text": "MENSA HARBURG",
        "amount": -3.6,
        "process": "Visa-Umsatz",
        "major": "Food & Drink",
        "minor": "Cafés & Snacks",
        "confidence": 0.85,
        "reason": "A university canteen at a small amount is a cheap prepared meal.",
    },
    {
        "id": 7,
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
        "id": 8,
        "text": "SPOTIFY AB",
        "amount": -4.99,
        "process": "Lastschrift / Belastung",
        "major": "Leisure & Entertainment",
        "minor": "Streaming & Media",
        "confidence": 0.96,
        "reason": "A music streaming subscription.",
    },
    {
        "id": 9,
        "text": "WWW.STEAMPOWERED.COM",
        "amount": -19.99,
        "process": "Lastschrift / Belastung",
        "major": "Leisure & Entertainment",
        "minor": "Games",
        "confidence": 0.94,
        "reason": "Steam sells computer games.",
    },
    {
        "id": 10,
        "text": "RIEKE FALKENBERG WEIHNACHTSGELD VON MAMA OMA OPA",
        "amount": 125.0,
        "process": "Übertrag / Überweisung",
        "major": "Gifts & Donations",
        "minor": "Gifts",
        "confidence": 0.78,
        "reason": "An incoming private transfer described as a Christmas present.",
    },
    {
        "id": 11,
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
        "id": 12,
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
        "id": 13,
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
    """
    The full hierarchy with its codes, in canonical order.

    The codes come from app.categories, which generates them from this same
    dictionary — the prompt, the validation and the reverse lookup cannot
    drift apart because there is only one table.
    """
    lines = []
    for major, minors in CATEGORY_HIERARCHY.items():
        lines.append(major)
        lines.extend(f"  {code_for(major, minor)}  {minor}" for minor in minors)
    return "\n".join(lines)


def render_examples(threshold: float) -> str:
    """
    The few-shot examples, shown in the answer format the model must produce.

    Below the threshold the example carries a reason, above it does not, so
    the rule is demonstrated rather than only described.
    """
    blocks = []
    for example in FEW_SHOT_EXAMPLES:
        code = code_for(example["major"], example["minor"])
        answer = {"id": example["id"], "c": code, "p": example["confidence"]}
        if example["confidence"] < threshold:
            answer["r"] = example["reason"]
        rendered = json.dumps(answer, ensure_ascii=False)[1:-1]
        blocks.append(
            f'{{"id": {example["id"]}, "text": "{example["text"]}", '
            f'"amount": {example["amount"]}, '
            f'"process": "{example["process"]}"}}\n'
            f"-> {{{rendered}}}"
            + (
                f"    # {example['major']} / {example['minor']}"
                if code
                else "    # invalid"
            )
        )
    return "\n\n".join(blocks)


def build_system_prompt(threshold: float) -> str:
    """
    The constant part of every request, ending at the cache breakpoint.

    Must not contain anything that varies between requests, or the cached
    prefix changes on every call and never gets a hit. The threshold is the
    one exception: it is configuration, constant for a run, and the model
    needs it to know when to justify itself.
    """
    return (
        f"{INSTRUCTIONS.format(threshold=threshold)}\n\n"
        f"# Category hierarchy\n\n{render_hierarchy()}\n\n"
        f"# Worked examples\n\n{render_examples(threshold)}\n"
    )


def build_response_schema() -> dict:
    """
    The JSON schema the response is constrained to.

    ``c`` is enumerated over the codes the hierarchy defines, so the model
    cannot name a pair that does not exist. ``r`` is optional: it is only
    wanted below the threshold, where somebody will actually read it.
    """
    return {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "c": {"type": "string", "enum": all_codes()},
                        # The API rejects minimum/maximum on a number, so the
                        # range is stated in the instructions and clamped by
                        # the client when the response is parsed.
                        "p": {"type": "number"},
                        "r": {"type": "string"},
                    },
                    "required": ["id", "c", "p"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    }
