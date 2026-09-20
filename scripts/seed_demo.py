#!/usr/bin/env python3
"""
Build a synthetic ledger for the screenshots in the README.

Every image in the README is of a real running instance, and the ledger behind
it is a bank statement — the one thing that must never be published. So the
screenshots are taken against an invented person instead: two years of a
plausible German current account, generated here from a fixed seed so the same
command produces the same pictures on any machine.

**It writes to its own database.** The name has to end in ``_demo`` or the
script refuses to run, because the two real databases in the same container
hold a household's finances and this one drops every collection it touches
before it writes. That guard is the whole reason the check is on the name
rather than on a flag.

**What the data has to contain is decided by what the pages look for.** A
generator emitting random merchants and random amounts produces a dashboard of
uniform noise, an empty trips page and nothing worth a finding, which is a
worse advertisement than no screenshot at all. So the ledger is built to
exercise the detectors:

*Schedules* — fifteen standing charges on fixed days, two of them with a price
rise part way through and one cancelled, so the recurring page has an active
run, a raised one and an ended one.

*Journeys* — three stretches where the card reports from another country,
written in the two formats Comdirect actually uses for that (a trailing
country code, and a converted amount with its rate). Two are confirmed trips
with their bookings and settlement tails assigned; the third is left
unconfirmed so the trips page shows a live proposal.

*Findings* — two rent rises, three subscriptions that went up, a utility's
annual settlement and a café habit drifting upwards over the last quarter.
Each is a real move in the numbers rather than a flag, so the pattern
detectors have to find them the same way they would in a real ledger.

*A review queue* — merchants nobody has filed before and booking lines that
hold no merchant at all, which is what the classifier declines to decide
rather than guess at.

Usage:
    DATABASE_NAME=bank_analyzer_demo python scripts/seed_demo.py --dry-run
    DATABASE_NAME=bank_analyzer_demo python scripts/seed_demo.py
"""

import argparse
import asyncio
import hashlib
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import hash_password  # noqa: E402
from app.categories import flow_type_for  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import db_manager  # noqa: E402
from app.models import (  # noqa: E402
    DIRECTION_IN,
    DIRECTION_OUT,
    SOURCE_LLM,
    MerchantCategory,
    Transaction,
    Trip,
    TripAssignment,
    User,
)
from app.normalization import merchant_key, normalize_merchant  # noqa: E402

#: The only databases this script will write to. It drops collections, and the
#: other two in the container are a real household's statements.
REQUIRED_SUFFIX = "_demo"

#: Login for the seeded account, so whoever takes the screenshots can sign in.
DEMO_USERNAME = "demo"
DEMO_PASSWORD = "demo"

#: Fixed so the README's images can be regenerated exactly.
SEED = 20260901

#: The window the invented statements cover. It ends mid-month on purpose:
#: statements arrive monthly, so a trailing partial month is the normal state
#: of the data and every detector has to survive it.
START = datetime(2024, 8, 1)
END = datetime(2026, 8, 14)

#: Comdirect's country code for the card's home country. Card lines end in
#: one, and the trip detector reads "abroad" as any code that is not the one
#: the card usually reports.
HOME_CODE = "276"

PROCESS_CARD = "Visa-Umsatz"
PROCESS_DEBIT = "Lastschrift / Belastung"
PROCESS_TRANSFER = "Übertrag / Überweisung"
PROCESS_ATM = "Auszahlung GAA"


def card_line(merchant: str, town: str, code: str = HOME_CODE) -> str:
    """A card payment as Comdirect writes it inside the euro area."""
    return f" {merchant} {town} KARTE 0132 {code}"


def converted_line(
    merchant: str, town: str, code: str, amount: float, rate: float, currency: str
) -> str:
    """A card payment abroad, with the figure it was converted from."""
    figure = f"{amount * rate:,.2f}"
    local = figure.replace(",", "X").replace(".", ",").replace("X", ".")
    return (
        f" {merchant} {town} KARTE 0132 {code}"
        f"Betrag: -{local} {currency}Kurs: {1 / rate:.6f}".replace(".", ",", 1)
    )


def debit_line(party: str, purpose: str) -> str:
    """A direct debit: the ordering party, then what it says it is for."""
    return (
        f"Auftraggeber: {party} Buchungstext: {purpose} "
        "Kto/IBAN: DE29500105175427896231"
    )


def transfer_line(party: str, purpose: str) -> str:
    """A credit transfer in or out."""
    return (
        f"Auftraggeber: {party} Buchungstext: {purpose} "
        "Kto/IBAN: DE44500105175407324931"
    )


def paypal_line(merchant: str, reference: str) -> str:
    """A PayPal debit that still names the shop behind it."""
    return (
        f"PP.4002.PP . {merchant}, Ihr Einkauf bei {merchant} "
        f"Ref. {reference}/0000000{reference[-2:]}"
    )


def truncated_paypal_line(reference: str) -> str:
    """
    A PayPal debit Comdirect truncated the payee out of.

    There is no merchant in this string for a rule or a model to read, which
    is exactly why the README has a section about recovering it from a PayPal
    export.
    """
    return f"PP.5189.PP . , Ihr Ei nkauf bei Ref. {reference}/00000004{reference[-2:]}"


class Ledger:
    """
    The invented statements, accumulated then written in one go.

    Amounts are held as they will be stored — negative for money leaving the
    account — and a transaction's ``flow_type`` is derived from its major
    category here exactly as it is everywhere else, so the demo cannot drift
    into a state the application would never produce.
    """

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.rows: list[dict] = []

    def add(
        self,
        when: datetime,
        amount: float,
        booking_text: str,
        process: str,
        major: str,
        minor: str,
        *,
        recurring: bool = False,
        confidence: float = 0.0,
        reason: str = "",
        source: str = SOURCE_LLM,
        tag: str = "",
    ) -> dict:
        row = {
            "date": when,
            "amount": round(amount, 2),
            "booking_text": booking_text,
            "process": process,
            "major": major,
            "minor": minor,
            "recurring": recurring,
            "confidence": confidence or round(self.rng.uniform(0.88, 0.99), 2),
            "reason": reason,
            "source": source,
            "tag": tag,
        }
        self.rows.append(row)
        return row

    def spend(
        self,
        when: datetime,
        amount: float,
        merchant: str,
        town: str,
        major: str,
        minor: str,
        **kwargs,
    ) -> dict:
        return self.add(
            when,
            -abs(amount),
            card_line(merchant, town),
            PROCESS_CARD,
            major,
            minor,
            **kwargs,
        )


#: Standing charges, in the order a month pays them. ``rise`` moves the price
#: from the given month onwards, which is what makes one detected schedule
#: show a raised price rather than two schedules; ``until`` ends a run, so the
#: recurring page has something that stopped as well as things that run.
STANDING = [
    dict(
        day=1,
        amount=1150.00,
        party="HAUSVERWALTUNG ALTONA GMBH",
        purpose="MIETE WOHNUNG 14",
        process=PROCESS_TRANSFER,
        major="Housing",
        minor="Rent",
        rise=("2025-10", 1210.00),
    ),
    dict(
        day=1,
        amount=49.00,
        party="DEUTSCHE BAHN DEUTSCHLANDTICKET",
        purpose="ABO DEUTSCHLANDTICKET",
        process=PROCESS_DEBIT,
        major="Transportation",
        minor="Public Transit",
        rise=("2025-01", 58.00),
    ),
    dict(
        day=2,
        amount=49.90,
        party="URBAN SPORTS GMBH",
        purpose="MITGLIEDSBEITRAG",
        process=PROCESS_DEBIT,
        major="Health & Personal Care",
        minor="Fitness & Sports",
        rise=("2025-07", 59.90),
    ),
    dict(
        day=3,
        amount=39.99,
        party="VODAFONE GMBH",
        purpose="KABEL INTERNET",
        process=PROCESS_DEBIT,
        major="Housing",
        minor="Internet & Phone",
    ),
    dict(
        day=4,
        amount=300.00,
        party="TRADE REPUBLIC BANK",
        purpose="SPARPLAN MSCI WORLD ISIN: IE00B4L5Y983",
        process=PROCESS_TRANSFER,
        major="Transfers & Savings",
        minor="Investments",
    ),
    dict(
        day=10,
        amount=12.90,
        party="HUK COBURG VERSICHERUNG",
        purpose="HAFTPFLICHT JAHRESBEITRAG",
        process=PROCESS_DEBIT,
        major="Financial",
        minor="Insurance",
    ),
    dict(
        day=12,
        amount=13.99,
        party="NETFLIX INTERNATIONAL BV",
        purpose="ABO STANDARD",
        process=PROCESS_DEBIT,
        major="Leisure & Entertainment",
        minor="Streaming & Media",
        rise=("2025-05", 19.99),
    ),
    dict(
        day=14,
        amount=8.99,
        party="DISNEY PLUS",
        purpose="ABO",
        process=PROCESS_DEBIT,
        major="Leisure & Entertainment",
        minor="Streaming & Media",
        until="2025-11",
    ),
    dict(
        day=15,
        amount=24.99,
        party="TELEKOM DEUTSCHLAND GMBH",
        purpose="MOBILFUNK RECHNUNG",
        process=PROCESS_DEBIT,
        major="Housing",
        minor="Internet & Phone",
        rise=("2026-02", 29.99),
    ),
    dict(
        day=18,
        amount=23.79,
        party="ADOBE SYSTEMS SOFTWARE",
        purpose="CREATIVE CLOUD FOTO",
        process=PROCESS_DEBIT,
        major="Leisure & Entertainment",
        minor="Software & Apps",
    ),
    dict(
        day=20,
        amount=2.99,
        party="APPLE SERVICES",
        purpose="ICLOUD SPEICHER",
        process=PROCESS_DEBIT,
        major="Leisure & Entertainment",
        minor="Software & Apps",
    ),
    dict(
        day=22,
        amount=20.00,
        party="AERZTE OHNE GRENZEN EV",
        purpose="FOERDERBEITRAG",
        process=PROCESS_DEBIT,
        major="Gifts & Donations",
        minor="Donations & Charity",
    ),
    dict(
        day=27,
        amount=210.40,
        party="TECHNIKER KRANKENKASSE",
        purpose="BEITRAG KRANKENVERSICHERUNG",
        process=PROCESS_DEBIT,
        major="Health & Personal Care",
        minor="Health Insurance",
        rise=("2026-01", 228.60),
    ),
    dict(
        day=28,
        amount=400.00,
        party="TAGESGELD RUECKLAGE",
        purpose="UEBERTRAG SPARKONTO",
        process=PROCESS_TRANSFER,
        major="Transfers & Savings",
        minor="Savings Contribution",
    ),
]

#: Spotify is billed through PayPal, so its booking line looks nothing like
#: the direct debits above. It is here to prove the schedule detector sees a
#: merchant through the processor rather than filing every PayPal line as one.
SPOTIFY = dict(day=8, amount=10.99, rise=("2025-09", 12.99))

#: The shops, weighted the way a week actually looks: groceries several times
#: over, everything else now and then.
GROCERS = [
    ("REWE SAGT DANKE.", "HAMBURG", 14, 62),
    ("EDEKA BOECKER", "HAMBURG", 12, 55),
    ("ALDI SUED SAGT DANKE", "HAMBURG", 9, 44),
    ("LIDL SAGT DANKE", "HAMBURG", 8, 40),
    ("BIO COMPANY", "HAMBURG", 11, 38),
]

DRUGSTORES = [
    ("DM DROGERIEMARKT", "HAMBURG", 6, 34),
    ("ROSSMANN 2200", "HAMBURG", 5, 28),
]

RESTAURANTS = [
    ("HANSA KEBAB", "HAMBURG", 9, 22),
    ("TRATTORIA DA ENZO", "HAMBURG", 24, 68),
    ("BURGERLICH", "HAMBURG", 18, 52),
    ("SUMUP  CAFE MIA", "HAMBURG", 14, 39),
    ("GOLDFISCH BAR", "HAMBURG", 16, 48),
]

CAFES = [
    ("BALZAC COFFEE", "HAMBURG", 3, 8),
    ("ELBGOLD ROESTEREI", "HAMBURG", 4, 11),
    ("BACKEREI JUNGE", "HAMBURG", 2, 7),
]

SHOPS = [
    ("AMAZON.DE", "Shopping", "General Merchandise", 9, 95),
    ("ZALANDO SE", "Shopping", "Clothing & Shoes", 25, 140),
    ("MEDIAMARKT", "Shopping", "Electronics & Tech", 30, 320),
    ("IKEA HAMBURG", "Housing", "Furniture & Household Goods", 20, 180),
    ("THALIA BUCHHANDLUNG", "Family & Education", "Books & Learning Materials", 12, 45),
    ("DECATHLON", "Shopping", "Sports & Outdoor Gear", 20, 110),
]

OCCASIONAL = [
    ("APOTHEKE AM MARKT", "Health & Personal Care", "Pharmacy & Medication", 8, 48),
    ("DB VERTRIEB GMBH", "Transportation", "Other Transportation", 29, 129),
    ("FREENOW TAXI", "Transportation", "Taxi & Sharing", 11, 32),
    ("MILES MOBILITY", "Transportation", "Taxi & Sharing", 14, 46),
    ("CINEMAXX HAMBURG", "Leisure & Entertainment", "Events & Culture", 12, 34),
    ("ELBPHILHARMONIE", "Leisure & Entertainment", "Events & Culture", 45, 95),
    ("FRISEUR KOPFSACHE", "Health & Personal Care", "Personal Care & Beauty", 32, 58),
]

DELIVERY = [("LIEFERANDO.DE", 16, 41), ("WOLT", 18, 44)]


def month_key(when: datetime) -> str:
    return f"{when.year:04d}-{when.month:02d}"


def months(start: datetime, end: datetime):
    """Every first-of-month between the two dates, inclusive."""
    year, month = start.year, start.month
    while datetime(year, month, 1) <= end:
        yield datetime(year, month, 1)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def on_day(anchor: datetime, day: int) -> datetime:
    """The given day of ``anchor``'s month, clamped to a day that exists."""
    for candidate in range(day, 0, -1):
        try:
            return anchor.replace(day=candidate)
        except ValueError:
            continue
    return anchor


#: The annual settlements a German utility sends once the meter is read. The
#: monthly figure is a flat instalment, so the year's real consumption arrives
#: as one charge in the spring — which is the shape the pattern detectors have
#: to survive without reporting a catastrophe every March.
SETTLEMENTS = [
    (datetime(2025, 3, 19), -184.60),
    (datetime(2026, 3, 18), -212.40),
]


def build_income(ledger: Ledger) -> None:
    """Salary, a raise, and freelance work that arrives when it arrives."""
    for first in months(START, END):
        pay_day = on_day(first, 28)
        if pay_day > END:
            continue
        amount = 3180.00 if month_key(first) < "2025-07" else 3395.00
        ledger.add(
            pay_day,
            amount,
            transfer_line(
                "NORDLICHT MEDIA GMBH", f"GEHALT {first.month:02d}/{first.year}"
            ),
            PROCESS_TRANSFER,
            "Income",
            "Salary & Wages",
            recurring=True,
            confidence=0.99,
            reason="A monthly credit from an employer.",
        )

    for offset in (2, 7, 11, 16, 20, 23):
        first = list(months(START, END))[offset]
        when = first + timedelta(days=ledger.rng.randint(4, 22))
        if when > END:
            continue
        ledger.add(
            when,
            ledger.rng.choice([420.0, 680.0, 950.0, 1250.0]),
            transfer_line("KONTORHAUS DESIGN UG", "RECHNUNG FREELANCE"),
            PROCESS_TRANSFER,
            "Income",
            "Self-Employment",
            confidence=0.93,
            reason="An invoice paid by a client.",
        )


def build_standing(ledger: Ledger) -> None:
    """The fifteen charges that arrive whether anybody spends anything or not."""
    for first in months(START, END):
        key = month_key(first)
        for charge in STANDING:
            if charge.get("until") and key >= charge["until"]:
                continue
            amount = charge["amount"]
            rise = charge.get("rise")
            if rise and key >= rise[0]:
                amount = rise[1]
            when = on_day(first, charge["day"])
            if when > END:
                continue
            if charge["minor"] == "Rent":
                text = transfer_line(charge["party"], charge["purpose"])
            elif charge["process"] == PROCESS_TRANSFER:
                text = transfer_line(charge["party"], charge["purpose"])
            else:
                text = debit_line(charge["party"], charge["purpose"])
            ledger.add(
                when,
                -amount,
                text,
                charge["process"],
                charge["major"],
                charge["minor"],
                recurring=True,
                confidence=0.97,
                reason="A charge that repeats on the same day every month.",
            )

        amount = SPOTIFY["amount"]
        if key >= SPOTIFY["rise"][0]:
            amount = SPOTIFY["rise"][1]
        when = on_day(first, SPOTIFY["day"])
        if when <= END:
            ledger.add(
                when,
                -amount,
                paypal_line("SPOTIFY AB", f"1{first.year}{first.month:02d}4471"),
                PROCESS_DEBIT,
                "Leisure & Entertainment",
                "Streaming & Media",
                recurring=True,
                confidence=0.96,
                reason="A music subscription billed monthly through PayPal.",
            )

        power_day = on_day(first, 5)
        if power_day <= END:
            ledger.add(
                power_day,
                -(96.00 if key < "2025-04" else 108.00),
                debit_line("HAMBURG ENERGIE GMBH", "ABSCHLAG STROM WAERME"),
                PROCESS_DEBIT,
                "Housing",
                "Utilities",
                recurring=True,
                confidence=0.95,
                reason="The monthly instalment for electricity and heating.",
            )

    for when, amount in SETTLEMENTS:
        ledger.add(
            when,
            amount,
            debit_line("HAMBURG ENERGIE GMBH", "JAHRESABRECHNUNG"),
            PROCESS_DEBIT,
            "Housing",
            "Utilities",
            confidence=0.94,
            reason="The yearly settlement once the meter has been read.",
        )


def away(when: datetime) -> bool:
    """
    Whether the cardholder was out of the country on this day.

    Everyday spending at home has to stop while somebody is abroad, or the
    trip page proposes a fortnight of Hamburg groceries as holiday spending —
    which is a plausible-looking screenshot of a thing that cannot happen.
    Standing charges are deliberately not covered: rent and insurance are
    collected whether anybody is home or not, and the trip suggester excluding
    them is one of the decisions worth showing.
    """
    windows = [(journey["start"], journey["end"]) for journey in JOURNEYS]
    windows.append((PROPOSAL["start"], PROPOSAL["end"]))
    return any(start <= when <= end for start, end in windows)


def build_everyday(ledger: Ledger) -> None:
    """
    Groceries, meals, errands — the volume the charts are actually made of.

    The café habit is the one series with a deliberate slope: it drifts up
    over the last quarter of the ledger so the pattern page has a drift to
    find that nothing else in the data explains.
    """
    rng = ledger.rng
    for first in months(START, END):
        key = month_key(first)
        days_in_month = (on_day(first, 31) - first).days + 1

        for _ in range(rng.randint(13, 18)):
            merchant, town, low, high = rng.choice(GROCERS)
            when = first + timedelta(days=rng.randrange(days_in_month))
            if when > END or away(when):
                continue
            ledger.spend(
                when,
                rng.uniform(low, high),
                merchant,
                town,
                "Food & Drink",
                "Groceries",
            )

        for _ in range(rng.randint(1, 3)):
            merchant, town, low, high = rng.choice(DRUGSTORES)
            when = first + timedelta(days=rng.randrange(days_in_month))
            if when > END or away(when):
                continue
            ledger.spend(
                when,
                rng.uniform(low, high),
                merchant,
                town,
                "Health & Personal Care",
                "Personal Care & Beauty",
            )

        for _ in range(rng.randint(4, 8)):
            merchant, town, low, high = rng.choice(RESTAURANTS)
            when = first + timedelta(days=rng.randrange(days_in_month))
            if when > END or away(when):
                continue
            ledger.spend(
                when,
                rng.uniform(low, high),
                merchant,
                town,
                "Food & Drink",
                "Restaurants & Bars",
            )

        drift = 1.0 + max(0.0, (key >= "2026-04") * 0.9)
        for _ in range(rng.randint(5, 9) + (4 if key >= "2026-04" else 0)):
            merchant, town, low, high = rng.choice(CAFES)
            when = first + timedelta(days=rng.randrange(days_in_month))
            if when > END or away(when):
                continue
            ledger.spend(
                when,
                rng.uniform(low, high) * drift,
                merchant,
                town,
                "Food & Drink",
                "Cafés & Snacks",
            )

        for _ in range(rng.randint(1, 4)):
            merchant, low, high = rng.choice(DELIVERY)
            when = first + timedelta(days=rng.randrange(days_in_month))
            if when > END or away(when):
                continue
            ledger.add(
                when,
                -rng.uniform(low, high),
                paypal_line(
                    merchant,
                    f"2{first.year}{first.month:02d}{rng.randint(10, 99)}",
                ),
                PROCESS_DEBIT,
                "Food & Drink",
                "Delivery & Takeout",
            )

        for _ in range(rng.randint(2, 5)):
            merchant, major, minor, low, high = rng.choice(SHOPS)
            when = first + timedelta(days=rng.randrange(days_in_month))
            if when > END or away(when):
                continue
            ledger.add(
                when,
                -rng.uniform(low, high),
                debit_line(merchant, "BESTELLUNG"),
                PROCESS_DEBIT,
                major,
                minor,
            )

        for _ in range(rng.randint(1, 3)):
            merchant, major, minor, low, high = rng.choice(OCCASIONAL)
            when = first + timedelta(days=rng.randrange(days_in_month))
            if when > END or away(when):
                continue
            ledger.spend(
                when, rng.uniform(low, high), merchant, "HAMBURG", major, minor
            )

        for _ in range(rng.randint(1, 2)):
            when = first + timedelta(days=rng.randrange(days_in_month))
            if when > END or away(when):
                continue
            ledger.add(
                when,
                -rng.choice([50.0, 100.0, 150.0, 200.0]),
                f"GA NR{rng.randint(10000, 99999)} BLZ20041133 "
                f"{when.day:02d}.{when.month:02d}/1{rng.randint(0, 9)}.3"
                f"{rng.randint(0, 9)}UHR HAMBURG {HOME_CODE}",
                PROCESS_ATM,
                "Transfers & Savings",
                "Cash Withdrawal",
                confidence=0.99,
                reason="Cash taken from a machine.",
            )

        if first.month == 12:
            for _ in range(rng.randint(4, 6)):
                merchant, major, minor, low, high = rng.choice(SHOPS)
                when = first + timedelta(days=rng.randrange(1, 22))
                if away(when):
                    continue
                ledger.add(
                    when,
                    -rng.uniform(60, 190),
                    debit_line(merchant, "BESTELLUNG GESCHENK"),
                    PROCESS_DEBIT,
                    "Gifts & Donations",
                    "Gifts",
                )


def build_refunds(ledger: Ledger) -> None:
    """Money coming back, which is not negative spending and is filed as such."""
    for when, amount, party in [
        (datetime(2024, 11, 19), 89.95, "ZALANDO SE"),
        (datetime(2025, 3, 6), 42.30, "AMAZON.DE"),
        (datetime(2025, 8, 27), 128.00, "TAP AIR PORTUGAL"),
        (datetime(2026, 5, 12), 64.99, "DECATHLON"),
    ]:
        ledger.add(
            when,
            amount,
            transfer_line(party, "GUTSCHRIFT RETOURE"),
            PROCESS_TRANSFER,
            "Income",
            "Refunds & Reimbursements",
            confidence=0.94,
            reason="An incoming amount from a shop is a refund.",
        )


#: The three stretches where the card stops reporting from home. ``code`` is
#: Comdirect's own country numbering; ``rate`` is euros per unit of the local
#: currency, and where it is None the charges are in euros and only the code
#: says anybody left the country.
JOURNEYS = [
    dict(
        name="Lisbon",
        destination="Lisbon, Portugal",
        code="620",
        currency=None,
        rate=None,
        start=datetime(2024, 9, 7),
        end=datetime(2024, 9, 15),
        split_ways=2,
        budget=1400.0,
        bookings=[
            (
                datetime(2024, 7, 20),
                238.40,
                "TAP AIR PORTUGAL",
                "Travel",
                "Flights & Long-Distance",
            ),
            (
                datetime(2024, 8, 2),
                612.00,
                "AIRBNB PAYMENTS UK",
                "Travel",
                "Accommodation",
            ),
        ],
        merchants=[
            ("PINGO DOCE", "LISBOA", "Food & Drink", "Groceries", 8, 34),
            ("A CEVICHERIA", "LISBOA", "Food & Drink", "Restaurants & Bars", 28, 74),
            ("PASTEIS DE BELEM", "LISBOA", "Food & Drink", "Cafés & Snacks", 4, 14),
            (
                "CARRIS METRO LISBOA",
                "LISBOA",
                "Transportation",
                "Public Transit",
                3,
                12,
            ),
            (
                "MUSEU CALOUSTE GULBEN",
                "LISBOA",
                "Leisure & Entertainment",
                "Events & Culture",
                10,
                26,
            ),
            ("TIME OUT MARKET", "LISBOA", "Food & Drink", "Restaurants & Bars", 16, 48),
            (
                "GETYOURGUIDE DEUTSC HLAND",
                "BERLIN",
                "Leisure & Entertainment",
                "Events & Culture",
                22,
                55,
            ),
        ],
        cash=[(datetime(2024, 9, 9), 150.0), (datetime(2024, 9, 13), 100.0)],
        tail=[
            (
                datetime(2024, 9, 18),
                46.20,
                "A CEVICHERIA",
                "LISBOA",
                "Food & Drink",
                "Restaurants & Bars",
            )
        ],
    ),
    dict(
        name="Stockholm",
        destination="Stockholm, Sweden",
        code="752",
        currency="SEK",
        rate=1 / 11.42,
        start=datetime(2025, 2, 14),
        end=datetime(2025, 2, 20),
        split_ways=1,
        budget=900.0,
        bookings=[
            (
                datetime(2025, 1, 8),
                186.98,
                "SCANDINAVIAN AIRLINES",
                "Travel",
                "Flights & Long-Distance",
            ),
            (
                datetime(2025, 1, 12),
                344.00,
                "BOOKING.COM BV",
                "Travel",
                "Accommodation",
            ),
        ],
        merchants=[
            ("ICA SUPERMARKET", "STOCKHOLM", "Food & Drink", "Groceries", 6, 28),
            (
                "PELIKAN RESTAURANG",
                "STOCKHOLM",
                "Food & Drink",
                "Restaurants & Bars",
                22,
                58,
            ),
            (
                "DROP COFFEE ROASTERS",
                "STOCKHOLM",
                "Food & Drink",
                "Cafés & Snacks",
                4,
                12,
            ),
            (
                "SL STOCKHOLM LOKALTRAFIK",
                "STOCKHOLM",
                "Transportation",
                "Public Transit",
                3,
                17,
            ),
            (
                "FOTOGRAFISKA",
                "STOCKHOLM",
                "Leisure & Entertainment",
                "Events & Culture",
                14,
                24,
            ),
            (
                "SYSTEMBOLAGET",
                "STOCKHOLM",
                "Food & Drink",
                "Other Food & Drink",
                11,
                32,
            ),
        ],
        cash=[(datetime(2025, 2, 16), 80.0)],
        tail=[
            (
                datetime(2025, 2, 24),
                31.40,
                "PELIKAN RESTAURANG",
                "STOCKHOLM",
                "Food & Drink",
                "Restaurants & Bars",
            )
        ],
    ),
    dict(
        name="Vietnam",
        destination="Vietnam",
        code="704",
        currency="VND",
        rate=1 / 27400,
        start=datetime(2026, 1, 24),
        end=datetime(2026, 2, 8),
        split_ways=1,
        budget=2300.0,
        bookings=[
            (
                datetime(2025, 10, 11),
                812.60,
                "VIETNAM AIRLINES",
                "Travel",
                "Flights & Long-Distance",
            ),
            (
                datetime(2025, 12, 3),
                468.00,
                "BOOKING.COM BV",
                "Travel",
                "Accommodation",
            ),
        ],
        merchants=[
            (
                "BANH MI HUYNH HOA",
                "HO CHI MINH",
                "Food & Drink",
                "Restaurants & Bars",
                4,
                16,
            ),
            (
                "BUN CHA HUONG LIEN",
                "HANOI",
                "Food & Drink",
                "Restaurants & Bars",
                6,
                22,
            ),
            ("PHO 10 LY QUOC SU", "HANOI", "Food & Drink", "Restaurants & Bars", 5, 14),
            ("BIA HOI CORNER", "HANOI", "Food & Drink", "Restaurants & Bars", 3, 19),
            ("HIGHLANDS COFFEE", "HANOI", "Food & Drink", "Cafés & Snacks", 2, 8),
            ("THE NOTE COFFEE", "HANOI", "Food & Drink", "Cafés & Snacks", 3, 9),
            (
                "BEN THANH MARKET",
                "HO CHI MINH",
                "Shopping",
                "General Merchandise",
                6,
                34,
            ),
            ("GRAB VIETNAM", "HANOI", "Transportation", "Taxi & Sharing", 3, 18),
            ("HANOI OLD QUARTER HOSTEL", "HANOI", "Travel", "Accommodation", 18, 42),
            ("VIETJET AIR", "HO CHI MINH", "Travel", "Flights & Long-Distance", 38, 74),
            ("HALONG BAY CRUISE CO", "HA LONG", "Travel", "Other Travel", 60, 145),
        ],
        cash=[(datetime(2026, 1, 26), 180.0), (datetime(2026, 2, 2), 160.0)],
        tail=[
            (
                datetime(2026, 2, 12),
                54.70,
                "VIETJET AIR",
                "HO CHI MINH",
                "Travel",
                "Flights & Long-Distance",
            )
        ],
    ),
]

#: A fourth journey, left unconfirmed on purpose: the trips page has to show
#: what a proposal looks like, and a page of nothing but finished trips hides
#: the half of the feature that does the work.
PROPOSAL = dict(
    code="203",
    currency="CZK",
    rate=1 / 25.2,
    start=datetime(2026, 6, 12),
    end=datetime(2026, 6, 15),
    merchants=[
        ("LOKAL DLOUHAAA", "PRAHA", "Food & Drink", "Restaurants & Bars", 12, 38),
        ("CAFE SAVOY", "PRAHA", "Food & Drink", "Cafés & Snacks", 5, 15),
        ("DOPRAVNI PODNIK PRAHA", "PRAHA", "Transportation", "Public Transit", 2, 9),
        ("HOTEL JULIS PRAHA", "PRAHA", "Travel", "Accommodation", 88, 132),
        ("KNIHKUPECTVI LUXOR", "PRAHA", "Shopping", "General Merchandise", 14, 30),
        ("PIVOVARSKY DUM", "PRAHA", "Food & Drink", "Restaurants & Bars", 16, 44),
    ],
)


def abroad_row(
    ledger: Ledger,
    when: datetime,
    amount: float,
    merchant: str,
    town: str,
    code: str,
    currency: str | None,
    rate: float | None,
    major: str,
    minor: str,
    tag: str,
) -> dict:
    """One charge made away from home, in whichever format the line needs."""
    if currency and rate:
        text = converted_line(merchant, town, code, amount, 1 / rate, currency)
    else:
        text = card_line(merchant, town, code)
    return ledger.add(when, -abs(amount), text, PROCESS_CARD, major, minor, tag=tag)


def build_journeys(ledger: Ledger) -> None:
    """The three stretches abroad, plus what was bought at home to get there."""
    rng = ledger.rng
    for journey in JOURNEYS:
        tag = journey["name"]
        for when, amount, party, major, minor in journey["bookings"]:
            ledger.add(
                when,
                -amount,
                debit_line(party, "BUCHUNG"),
                PROCESS_DEBIT,
                major,
                minor,
                confidence=0.96,
                reason="A travel booking made ahead of the journey.",
                tag=tag,
            )

        span = (journey["end"] - journey["start"]).days
        for day in range(span + 1):
            when = journey["start"] + timedelta(days=day)
            for _ in range(rng.randint(2, 4)):
                merchant, town, major, minor, low, high = rng.choice(
                    journey["merchants"]
                )
                abroad_row(
                    ledger,
                    when,
                    rng.uniform(low, high),
                    merchant,
                    town,
                    journey["code"],
                    journey["currency"],
                    journey["rate"],
                    major,
                    minor,
                    tag,
                )

        for when, amount in journey["cash"]:
            local = ""
            if journey["currency"] and journey["rate"]:
                local = (
                    f"Betrag: -{amount / journey['rate']:,.2f} {journey['currency']}"
                    f"Kurs: {journey['rate']:.8f}"
                )
            ledger.add(
                when,
                -amount,
                f"GA NR{rng.randint(10000, 99999)} BLZ00000000 "
                f"{when.day:02d}.{when.month:02d} {journey['code']}{local}",
                PROCESS_ATM,
                "Transfers & Savings",
                "Cash Withdrawal",
                confidence=0.99,
                reason="Cash taken from a machine abroad.",
                tag=tag,
            )

        for when, amount, merchant, town, major, minor in journey["tail"]:
            abroad_row(
                ledger,
                when,
                amount,
                merchant,
                town,
                journey["code"],
                journey["currency"],
                journey["rate"],
                major,
                minor,
                tag,
            )

    span = (PROPOSAL["end"] - PROPOSAL["start"]).days
    for day in range(span + 1):
        when = PROPOSAL["start"] + timedelta(days=day)
        for _ in range(2):
            merchant, town, major, minor, low, high = rng.choice(PROPOSAL["merchants"])
            abroad_row(
                ledger,
                when,
                rng.uniform(low, high),
                merchant,
                town,
                PROPOSAL["code"],
                PROPOSAL["currency"],
                PROPOSAL["rate"],
                major,
                minor,
                "",
            )


#: Lines that carry a merchant nobody has seen before. These are what the
#: review queue is really for: filing one writes the merchant cache, so the
#: next payment to the same shop never reaches the API at all.
UNDECIDED_MERCHANTS = [
    (
        datetime(2026, 7, 31),
        -18.40,
        "SUMUP  WOCHENMARKT STAND 14",
        "HAMBURG",
        0.21,
        "A market stall billed through a card terminal, with nothing saying what "
        "it sells.",
    ),
    (
        datetime(2026, 7, 11),
        -142.00,
        "MONDIAL SERVICE GMBH",
        "",
        0.26,
        "The ordering party is a service company whose trade the line does not name.",
    ),
    (
        datetime(2026, 6, 20),
        -63.50,
        "K. WERNICKE HANDWERK",
        "",
        0.33,
        "A tradesperson's invoice could be a repair or a purchase.",
    ),
]

#: Transfers between people, which say nothing about what they were for. The
#: classifier will not guess and the queue is where they land.
UNDECIDED_TRANSFERS = [
    (datetime(2026, 7, 25), -60.00, "M. KELLERMANN", "RUECKZAHLUNG", 0.28),
    (datetime(2026, 7, 8), 145.00, "J. ARNDT", "WIE BESPROCHEN", 0.35),
    (datetime(2026, 6, 24), -35.00, "L. BRENNER", "DANKE", 0.41),
]

#: Booking lines that hold no merchant at all. Comdirect truncated the payee
#: out of them, so they are here to show what the PayPal recovery in the
#: README is for, and they sit at the foot of the queue.
UNDECIDED_PAYPAL = [
    (datetime(2026, 7, 29), -34.90, "4712", 0.44),
    (datetime(2026, 7, 21), -12.49, "4709", 0.47),
    (datetime(2026, 7, 14), -78.00, "4702", 0.51),
]


def build_queue(ledger: Ledger) -> None:
    """The transactions the classifier declined to decide."""
    for when, amount, merchant, town, confidence, reason in UNDECIDED_MERCHANTS:
        text = (
            card_line(merchant, town)
            if town
            else debit_line(
                merchant, f"KUNDENNUMMER {ledger.rng.randint(10000, 99999)}"
            )
        )
        ledger.add(
            when,
            amount,
            text,
            PROCESS_CARD if town else PROCESS_DEBIT,
            "Uncategorized",
            "Needs Review",
            confidence=confidence,
            reason=reason,
        )
    for when, amount, party, purpose, confidence in UNDECIDED_TRANSFERS:
        ledger.add(
            when,
            amount,
            transfer_line(party, purpose),
            PROCESS_TRANSFER,
            "Uncategorized",
            "Needs Review",
            confidence=confidence,
            reason="A transfer between two people says nothing about its purpose.",
        )
    for when, amount, reference, confidence in UNDECIDED_PAYPAL:
        ledger.add(
            when,
            amount,
            truncated_paypal_line(reference),
            PROCESS_DEBIT,
            "Uncategorized",
            "Needs Review",
            confidence=confidence,
            reason="The booking line names no merchant.",
        )


def transaction_of(row: dict, user_id) -> Transaction:
    """One generated row as the document the application would have stored."""
    combined = f"{row['date']}{row['amount']}{row['booking_text']}{row['process']}"
    return Transaction(
        user_id=user_id,
        hash=hashlib.md5(combined.encode()).hexdigest(),
        date=row["date"],
        amount=row["amount"],
        booking_text=row["booking_text"],
        process=row["process"],
        major_category=row["major"],
        minor_category=row["minor"],
        flow_type=flow_type_for(row["major"]),
        is_recurring=row["recurring"],
        confidence=row["confidence"],
        reason=row["reason"] or None,
        classification_source=row["source"],
    )


def merchant_entries(rows: list[dict], user_id) -> list[MerchantCategory]:
    """
    The cache the classifier would hold after filing all of this.

    Built from the transactions rather than listed separately, so the demo
    cannot contain a merchant whose remembered category disagrees with the
    transactions it was learned from.
    """
    seen: dict[tuple[str, str], MerchantCategory] = {}
    for row in rows:
        if row["minor"] == "Needs Review":
            continue
        name = normalize_merchant(row["booking_text"], row["process"])
        if not name:
            continue
        direction = DIRECTION_IN if row["amount"] > 0 else DIRECTION_OUT
        key = (merchant_key(name), direction)
        entry = seen.get(key)
        if entry is None:
            seen[key] = MerchantCategory(
                user_id=user_id,
                merchant_key=key[0],
                display_name=name,
                direction=direction,
                major=row["major"],
                minor=row["minor"],
                source=SOURCE_LLM,
                confidence=row["confidence"],
                reason=row["reason"] or None,
                hit_count=1,
                first_seen=row["date"],
                last_used=row["date"],
                model_version="claude-haiku-4-5",
            )
        else:
            entry.hit_count += 1
            entry.last_used = max(entry.last_used, row["date"])
    return list(seen.values())


async def write(rows: list[dict]) -> dict:
    """Replace the demo database's contents with this ledger."""
    for model in (Transaction, MerchantCategory, Trip, TripAssignment, User):
        await model.find_all().delete()

    user = await User(
        username=DEMO_USERNAME, password_hash=hash_password(DEMO_PASSWORD)
    ).insert()

    documents = [transaction_of(row, user.id) for row in rows]
    await Transaction.insert_many(documents)

    stored = {
        document.hash: document for document in await Transaction.find_all().to_list()
    }
    by_row = {id(row): stored[document.hash] for row, document in zip(rows, documents)}

    entries = merchant_entries(rows, user.id)
    if entries:
        await MerchantCategory.insert_many(entries)

    trips = 0
    assignments = 0
    for journey in JOURNEYS:
        trip = Trip(
            user_id=user.id,
            name=journey["name"],
            destination=journey["destination"],
            start_date=journey["start"],
            end_date=journey["end"],
            budget=journey["budget"],
            split_ways=journey["split_ways"],
        )
        await trip.insert()
        trips += 1
        members = [row for row in rows if row["tag"] == journey["name"]]
        for row in members:
            await TripAssignment(
                user_id=user.id,
                trip_id=str(trip.id),
                transaction_id=str(by_row[id(row)].id),
                decision="included",
                share=1.0,
            ).insert()
            assignments += 1

    return {
        "transactions": len(documents),
        "merchants": len(entries),
        "trips": trips,
        "assignments": assignments,
    }


def separate(rows: list[dict]) -> None:
    """
    Move a cent where two generated rows would hash to the same transaction.

    A transaction's identity is the hash of its date, amount, booking line and
    process, which is what makes re-importing an overlapping statement
    harmless. Two random lunches at the same restaurant on the same day for
    the same amount are therefore one transaction as far as the application is
    concerned, and the demo has to respect that rather than work around it in
    the database.
    """
    seen: set[tuple] = set()
    for row in rows:
        while (
            key := (row["date"], row["amount"], row["booking_text"], row["process"])
        ) in seen:
            row["amount"] = round(row["amount"] - 0.01, 2)
        seen.add(key)


def build() -> list[dict]:
    """The whole invented ledger, oldest first."""
    ledger = Ledger(random.Random(SEED))
    build_income(ledger)
    build_standing(ledger)
    build_everyday(ledger)
    build_refunds(ledger)
    build_journeys(ledger)
    build_queue(ledger)
    ledger.rows.sort(key=lambda row: row["date"])
    separate(ledger.rows)
    return ledger.rows


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be written without touching the database",
    )
    args = parser.parse_args()

    if not settings.database_name.endswith(REQUIRED_SUFFIX):
        print(
            f"Refusing to seed {settings.database_name!r}: this script drops "
            f"collections and will only write to a database whose name ends in "
            f"{REQUIRED_SUFFIX!r}.",
            file=sys.stderr,
        )
        return 1

    rows = build()
    spend = sum(-row["amount"] for row in rows if row["amount"] < 0)
    income = sum(row["amount"] for row in rows if row["amount"] > 0)
    print(
        f"{len(rows)} transactions, {rows[0]['date']:%Y-%m-%d} to "
        f"{rows[-1]['date']:%Y-%m-%d}, {income:,.0f} EUR in and {spend:,.0f} EUR out."
    )

    if args.dry_run:
        print(f"Dry run: {settings.database_name} untouched.")
        return 0

    await db_manager.connect()
    try:
        result = await write(rows)
    finally:
        await db_manager.disconnect()

    print(
        f"Wrote {result['transactions']} transactions, {result['merchants']} "
        f"merchant entries, {result['trips']} trips and {result['assignments']} "
        f"trip assignments to {settings.database_name}."
    )
    print(f"Log in as {DEMO_USERNAME!r} / {DEMO_PASSWORD!r}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
