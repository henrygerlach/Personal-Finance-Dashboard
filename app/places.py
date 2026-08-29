"""
Turning what somebody typed into a point on a map.

A trip's destination is a free-text field, and it has to stay one: "Portugal",
"Lisbon", "Lissabon", "Cornwall", "Bali", "Portugal & Spain" and "Lisbon,
Portugal" are all things a person reasonably writes, and none of them is worth
replacing with a dropdown of two hundred countries and no towns. So the
resolving happens here, against a gazetteer built from Natural Earth and
committed to the repository (see ``scripts/build_world_data.py``).

**Why not a geocoding service.** Every other way of doing this sends the
destination to somebody else's server. This application reads bank statements;
handing a third party the list of places its user has been on holiday, in
exchange for a coordinate that has not moved since the eighteenth century, is
a bad trade. The gazetteer is 700 KB and answers offline.

**What it will not do is guess in silence.** A lookup returns the name it
matched, not just the coordinate, so the map can say which Springfield it
chose — and a destination that resolves to nothing is reported as unresolved
rather than dropped, because a trip missing from a map of every trip is a bug
the reader cannot see.

**Order of preference.** A name claimed by more than one place goes to a
country first, then to the largest city, then to a region: somebody who writes
"Mexico" means the country, and somebody who writes "Paris" means the one in
France rather than the one in Texas. That ordering is baked into the row order
of the data file, so resolving a collision is taking the lowest row number.
"""

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

#: The gazetteer, next to this module rather than in ``static``: it is read by
#: the server and never served to the browser.
DATA_FILE = Path(__file__).parent / "data" / "places.json"

#: Row layout in the data file. Objects with named keys would have made two
#: thirds of a 10,000-row file be the key names.
_KIND, _NAME, _CODE, _LAT, _LON = range(5)

KIND_NAMES = {0: "country", 1: "city", 2: "region"}

#: What separates two destinations in one field. A trip really can go to two
#: places, and "Portugal & Spain" deserves two dots rather than none.
SEPARATORS = re.compile(r"\s*(?:[,;/+&·]|\band\b|\bund\b|\bto\b|[–—])\s*")

#: Below this many characters a word is not looked up on its own. Without it,
#: trimming a phrase down to its last resort matches two-letter country codes
#: inside ordinary words and puts a dot in the Balkans for "a week in Kos".
MIN_FRAGMENT = 3


@dataclass(frozen=True)
class Place:
    """One resolved location, with the name it was actually matched against."""

    name: str
    country: str
    code: str
    kind: str
    latitude: float
    longitude: float


@lru_cache(maxsize=1)
def _gazetteer() -> dict:
    """The data file, parsed once per process."""
    return json.loads(DATA_FILE.read_text())


def normalize(text: str) -> str:
    """
    A name reduced to what two spellings of it have in common.

    Accents go, because a keyboard may not have them; case goes; punctuation
    goes; and a leading article goes, because "The Netherlands" and
    "Netherlands" are one country. This must agree exactly with the
    normalisation the build script applied to the index, so the two live as
    copies of one another on purpose — the alternative is importing a script
    into the application.
    """
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    folded = folded.lower().replace("ß", "ss")
    folded = re.sub(r"[^a-z0-9]+", " ", folded).strip()
    folded = re.sub(r"^(the|le|la|les|el|il|de|der|die|das) ", "", folded)
    return folded


def _place_at(position: int) -> Place:
    """Build a :class:`Place` from one row of the data file."""
    data = _gazetteer()
    row = data["places"][position]
    return Place(
        name=row[_NAME],
        country=data["countries"].get(row[_CODE], ""),
        code=row[_CODE],
        kind=KIND_NAMES[row[_KIND]],
        latitude=row[_LAT],
        longitude=row[_LON],
    )


def _lookup(fragment: str) -> Place | None:
    """The best place a normalised fragment names, if it names one at all."""
    positions = _gazetteer()["index"].get(fragment)
    if not positions:
        return None
    # Lowest row number wins: the file is ordered by preference.
    return _place_at(min(positions))


def _longest_match(fragment: str) -> Place | None:
    """
    The longest run of words in a fragment that names somewhere.

    For "Lisbon Portugal", written without the comma that would have split it,
    the whole fails and "Lisbon" wins — longest first, so a two-word name is
    never broken up by a one-word match inside it.
    """
    words = fragment.split()
    for width in range(len(words), 0, -1):
        for start in range(0, len(words) - width + 1):
            window = " ".join(words[start : start + width])
            if len(window) < MIN_FRAGMENT:
                continue
            found = _lookup(window)
            if found:
                return found
    return None


def locate(destination: str | None) -> list[Place]:
    """
    Every place a destination field names, most specific first.

    Args:
        destination: What the traveller wrote, in any of the shapes a free
            text field attracts.

    Returns:
        The places named, which is usually one and is empty when nothing in
        the text is a place this gazetteer knows. A country is dropped from
        the result when something more specific inside it also matched, so
        that "Lisbon, Portugal" is one dot in Lisbon rather than two.
    """
    text = (destination or "").strip()
    if not text:
        return []

    if len(normalize(text)) < MIN_FRAGMENT:
        return []

    whole = _lookup(normalize(text))
    if whole:
        return [whole]

    found: list[Place] = []
    for part in SEPARATORS.split(text):
        fragment = normalize(part)
        if not fragment:
            continue
        place = _lookup(fragment) or _longest_match(fragment)
        if place and place not in found:
            found.append(place)

    specific = {place.code for place in found if place.kind != "country"}
    return [
        place
        for place in found
        if place.kind != "country" or place.code not in specific
    ]


def atlas(trips: list[dict]) -> dict:
    """
    Every trip as a point on a map, and the ones that could not be placed.

    Trips at the same place are one point: three weekends in Lisbon are one
    dot carrying three trips, not three dots on top of each other. A trip
    whose destination names two places — "Portugal & Spain" — is on the map
    twice, once in each, because it was.

    Args:
        trips: Overview rows, each with ``id``, ``name``, ``destination``,
            the two dates and a ``summary``.

    Returns:
        ``points`` sorted by what was spent there, largest first, so that a
        greedy label placer gives the labels to the trips that matter most;
        ``countries`` as the ISO codes to shade in; and ``unplaced``, every
        trip that is not on the map — one with no destination written down,
        and one whose destination named nowhere the gazetteer knows. A trip
        missing from a map of all of them is a thing the reader cannot see,
        so it is named under the map rather than swallowed.
    """
    points: dict = {}
    unplaced = []
    for trip in trips:
        destination = (trip.get("destination") or "").strip()
        found = locate(destination)
        if not found:
            # Named by its destination where there is one, because that is
            # the field to correct; by its own name where there is not,
            # because that is the field to fill in.
            unplaced.append({"label": destination or trip["name"]})
            continue
        for place in found:
            key = f"{place.name}|{place.code}|{place.latitude}|{place.longitude}"
            point = points.setdefault(
                key,
                {
                    "place": place.name,
                    "country": place.country,
                    "code": place.code,
                    "lat": place.latitude,
                    "lon": place.longitude,
                    "net": 0.0,
                    "trips": [],
                },
            )
            point["net"] += trip["summary"]["net"]
            point["trips"].append(
                {
                    "id": trip["id"],
                    "name": trip["name"],
                    "start_date": trip["start_date"],
                    "end_date": trip["end_date"],
                    "net": trip["summary"]["net"],
                }
            )

    ordered = sorted(points.values(), key=lambda point: -point["net"])
    for point in ordered:
        point["net"] = round(point["net"], 2)

    return {
        "points": ordered,
        "countries": sorted({point["code"] for point in ordered if point["code"]}),
        "unplaced": unplaced,
    }
