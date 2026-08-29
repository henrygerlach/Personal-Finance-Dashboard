"""
Vendor the two data files the trips map is drawn from.

The map has to work with no network and no map service: this dashboard reads
somebody's bank statements, and asking a tile server for the squares of the
world containing their holidays would hand a third party a coarse travel
history for nothing. So the coastlines are drawn from geometry stored in the
repository, and a destination is turned into a coordinate by a gazetteer
stored next to it, rather than by a geocoding API.

Both files come from Natural Earth (public domain), and this script is how
they are made. It is not run by the app — it is run by hand when the data
needs refreshing, and its output is committed:

    poetry run python scripts/build_world_data.py

**app/static/world.json** — country outlines at 1:110m, simplified further
here, as raw longitude/latitude. The browser projects them, because it has to
project the trip dots too and the two must agree; shipping pre-projected paths
would put the projection in two places and let them drift.

**app/data/places.json** — every country, region and town of any size, with
the names each is known by, pointing at a coordinate. Names in seven European
languages are kept for countries and larger cities, because "Lissabon",
"Mailand" and "Florenz" are what a German-speaking traveller types.
"""

import json
import math
import re
import sys
import unicodedata
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CACHE = REPO / ".natural-earth-cache"
WORLD_OUT = REPO / "app" / "static" / "world.json"
PLACES_OUT = REPO / "app" / "data" / "places.json"

SOURCE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson"

#: How far a coastline point may be moved before it is dropped, in degrees.
#: A world map three pixels to the degree makes this a half-pixel error, and
#: it is still under two pixels when the frame closes in on one continent.
SIMPLIFY_TOLERANCE = 0.15

#: Islands smaller than this, in square degrees, are dropped. At world scale
#: they are a speck of noise; the ones that are somebody's holiday — Malta,
#: the Balearics — are in the gazetteer and get a dot of their own.
MIN_RING_AREA = 0.6

#: Antarctica is dropped and the map is cut off below this, which keeps the
#: frame on the inhabited world instead of spending a third of its height on
#: ice nobody has a hotel booking for.
SOUTH_LIMIT = -58.0

#: Where a city stops being a town that happens to share a name and becomes
#: the thing the name means. Above it a city outranks a region of the same
#: name; below it the region wins.
MAJOR_CITY_POPULATION = 300_000

#: A town this size is worth carrying even if nothing else marks it out.
#: Below it, the gazetteer keeps only capitals and places Natural Earth
#: itself flags as of more than local importance.
MIN_CITY_POPULATION = 100_000

#: Where multilingual names are worth their size. Countries and big cities
#: get called different things in different languages; a town of forty
#: thousand is called the same thing everywhere it is called anything.
NAME_LANGUAGES = ("EN", "DE", "FR", "ES", "IT", "NL", "PT")

#: The colloquial names no dataset carries, because they are not anybody's
#: official name for anywhere.
COUNTRY_NICKNAMES = {
    "GBR": ["UK", "Great Britain", "Britain", "England", "Grossbritannien"],
    "USA": ["USA", "US", "America", "The States", "Vereinigte Staaten"],
    "NLD": ["Holland"],
    "ARE": ["UAE"],
    "CZE": ["Czech Republic", "Tschechien"],
    "CHE": ["Switzerland", "Schweiz"],
    "KOR": ["South Korea", "Suedkorea"],
    "MMR": ["Burma"],
    "CIV": ["Ivory Coast"],
    "CPV": ["Cape Verde", "Kapverden"],
    "SWZ": ["Swaziland"],
    "TLS": ["East Timor"],
    "VAT": ["Vatican"],
}

#: The places a gazetteer of countries, provinces and towns does not contain,
#: because they are islands, coastlines and landmarks rather than units of
#: administration — and the exonyms no dataset carries for a region, which
#: matter here because the traveller writing the destination may not be
#: writing in English. Each is a name, a coordinate, the country it is in, and
#: the other names it goes by. They outrank everything but a country, so
#: "Ibiza" is the island rather than the town on it.
EXTRA_PLACES = [
    ("Mallorca", 39.62, 2.95, "ESP", ["Majorca"]),
    ("Menorca", 39.98, 4.11, "ESP", ["Minorca"]),
    ("Ibiza", 38.98, 1.43, "ESP", ["Eivissa"]),
    ("Tenerife", 28.29, -16.63, "ESP", []),
    ("Gran Canaria", 27.96, -15.60, "ESP", []),
    ("Lanzarote", 29.03, -13.63, "ESP", []),
    ("Fuerteventura", 28.36, -14.05, "ESP", []),
    (
        "Canary Islands",
        28.30,
        -15.90,
        "ESP",
        ["Islas Canarias", "Kanaren", "Kanarische Inseln"],
    ),
    ("Costa Brava", 41.85, 3.08, "ESP", []),
    ("Costa del Sol", 36.55, -4.70, "ESP", []),
    ("Andalusia", 37.40, -4.80, "ESP", ["Andalusien"]),
    ("Catalonia", 41.80, 1.70, "ESP", ["Katalonien", "Catalunya"]),
    ("Sardinia", 40.10, 9.10, "ITA", ["Sardegna", "Sardinien"]),
    ("Sicily", 37.60, 14.00, "ITA", ["Sizilien"]),
    ("Amalfi Coast", 40.63, 14.60, "ITA", ["Costiera Amalfitana"]),
    ("Lake Garda", 45.63, 10.68, "ITA", ["Gardasee", "Lago di Garda"]),
    ("Crete", 35.20, 24.90, "GRC", ["Kreta"]),
    ("Santorini", 36.40, 25.46, "GRC", ["Thira"]),
    ("Mykonos", 37.45, 25.35, "GRC", []),
    ("Kos", 36.85, 27.24, "GRC", []),
    ("Corfu", 39.62, 19.92, "GRC", ["Korfu", "Kerkyra"]),
    ("Zakynthos", 37.79, 20.90, "GRC", ["Zante"]),
    ("Provence", 43.90, 5.90, "FRA", []),
    ("Cote d'Azur", 43.60, 7.00, "FRA", ["French Riviera", "Riviera"]),
    ("Normandy", 49.10, 0.10, "FRA", ["Normandie"]),
    ("Brittany", 48.20, -2.90, "FRA", ["Bretagne"]),
    ("Tyrol", 47.25, 11.40, "AUT", ["Tirol"]),
    ("Black Forest", 48.30, 8.20, "DEU", ["Schwarzwald"]),
    ("England", 52.50, -1.50, "GBR", []),
    ("Scotland", 56.50, -4.20, "GBR", ["Schottland"]),
    ("Lake District", 54.50, -3.10, "GBR", []),
    ("Patagonia", -45.00, -70.00, "ARG", ["Patagonien"]),
    ("Machu Picchu", -13.16, -72.55, "PER", []),
    ("Galapagos", -0.70, -90.40, "ECU", ["Galapagos Islands"]),
    ("Yellowstone", 44.60, -110.50, "USA", []),
    ("Yosemite", 37.80, -119.50, "USA", []),
    ("Grand Canyon", 36.10, -112.10, "USA", []),
    ("Banff", 51.18, -115.57, "CAN", []),
    ("Bora Bora", -16.50, -151.74, "PYF", []),
    ("Koh Samui", 9.51, 100.01, "THA", ["Ko Samui"]),
    ("Serengeti", -2.33, 34.83, "TZA", []),
    ("Kilimanjaro", -3.07, 37.35, "TZA", []),
]

#: What a place is, stored as a small integer because it is repeated ten
#: thousand times. The order is also the order of preference: a name claimed
#: by both a country and a town belongs to the country, because a traveller
#: who writes "Mexico" means the country.
KIND_COUNTRY = 0
KIND_CITY = 1
KIND_REGION = 2


def fetch(name: str) -> dict:
    """Read one Natural Earth layer, downloading it once into a cache."""
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"{name}.geojson"
    if not path.exists():
        print(f"  downloading {name}", file=sys.stderr)
        urllib.request.urlretrieve(f"{SOURCE}/{name}.geojson", path)
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Coastlines
# ---------------------------------------------------------------------------


def ring_area(ring: list) -> float:
    """The shoelace area of a ring, in square degrees, sign discarded."""
    total = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        total += x1 * y2 - x2 * y1
    return abs(total) / 2


def simplify(ring: list, tolerance: float) -> list:
    """
    Douglas-Peucker, iteratively so a long coastline cannot blow the stack.

    Returns:
        The ring with every point that sits within ``tolerance`` of the line
        between its kept neighbours removed.
    """
    if len(ring) < 3:
        return ring
    keep = [False] * len(ring)
    keep[0] = keep[-1] = True
    stack = [(0, len(ring) - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        (x1, y1), (x2, y2) = ring[first], ring[last]
        dx, dy = x2 - x1, y2 - y1
        span = math.hypot(dx, dy)
        worst, at = -1.0, first
        for index in range(first + 1, last):
            x, y = ring[index]
            if span == 0:
                distance = math.hypot(x - x1, y - y1)
            else:
                distance = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / span
            if distance > worst:
                worst, at = distance, index
        if worst > tolerance:
            keep[at] = True
            stack.append((first, at))
            stack.append((at, last))
    return [point for point, kept in zip(ring, keep) if kept]


def build_world() -> dict:
    """
    The country outlines, as flat longitude/latitude arrays.

    Each country keeps its own rings rather than being melted into one land
    mass, because the map fills the countries that have been visited and that
    needs a shape per country to fill.
    """
    data = fetch("ne_110m_admin_0_countries")
    countries = []
    points = 0
    for feature in data["features"]:
        code = feature["properties"]["ADM0_A3"]
        if code == "ATA":
            continue
        geometry = feature["geometry"]
        polygons = (
            geometry["coordinates"]
            if geometry["type"] == "MultiPolygon"
            else [geometry["coordinates"]]
        )
        rings = []
        for polygon in polygons:
            # Only the outer ring of each polygon. Holes at this scale are
            # Lesotho and the Caspian, and an unfilled hole costs less than
            # the winding rules needed to punch one.
            outer = polygon[0]
            if ring_area(outer) < MIN_RING_AREA:
                continue
            simplified = simplify(outer, SIMPLIFY_TOLERANCE)
            if len(simplified) < 3:
                continue
            flat = []
            for longitude, latitude in simplified:
                flat.append(round(longitude, 1))
                flat.append(round(max(latitude, SOUTH_LIMIT), 1))
            rings.append(flat)
            points += len(simplified)
        if rings:
            countries.append({"id": code, "rings": rings})
    print(f"  {len(countries)} countries, {points} points", file=sys.stderr)
    return {"south_limit": SOUTH_LIMIT, "countries": countries}


# ---------------------------------------------------------------------------
# Gazetteer
# ---------------------------------------------------------------------------


def normalize(text: str) -> str:
    """
    A name reduced to what two spellings of it have in common.

    Accents go, because a keyboard may not have them; case goes; punctuation
    and the joining words that survive translation unevenly go too. What is
    left is the key both "Cote d'Ivoire" and "Cote dIvoire" arrive at.
    """
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    folded = folded.lower().replace("ß", "ss")
    folded = re.sub(r"[^a-z0-9]+", " ", folded).strip()
    folded = re.sub(r"^(the|le|la|les|el|il|de|der|die|das) ", "", folded)
    return folded


def index_keys(name: str) -> list:
    """
    Every spelling of a name worth indexing under.

    Stripping the accents off "München" gives "Munchen", which is not what a
    German keyboard without an umlaut produces — "Muenchen" is. Both are
    indexed, so either typing finds the city.
    """
    keys = [normalize(name)]
    transliterated = (
        name.replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("Ä", "Ae")
        .replace("Ö", "Oe")
        .replace("Ü", "Ue")
    )
    if transliterated != name:
        keys.append(normalize(transliterated))
    return [key for key in dict.fromkeys(keys) if key]


def alias_values(properties: dict, keys: list) -> list:
    """
    Every name under ``keys``, splitting the pipe-separated lists.

    Abbreviations are thrown away: the alternates carry "IN" and "Ind." for
    Indiana, and a gazetteer that answers "in" with a point in the American
    midwest will put a dot there for "a week in the Alps". Nowhere on earth
    has a two-letter name, so the length is a safe test — the handful of
    initialisms worth knowing are added by hand in COUNTRY_NICKNAMES.
    """
    found = []
    for key in keys:
        value = properties.get(key)
        if not value or not isinstance(value, str):
            continue
        for part in value.split("|"):
            part = part.strip()
            if len(part) > 2 and not part.endswith("."):
                found.append(part)
    return found


def build_places() -> dict:
    """
    Names to coordinates: countries, regions, and towns of any size.

    Returns:
        ``{"countries": {code: name}, "places": [row, ...], "index":
        {normalised name: [row number, ...]}}``, where a row is ``[kind,
        name, country code, latitude, longitude]``. Rows rather than objects
        because there are ten thousand of them and the key names would be
        two thirds of the file.

        The rows are ordered by preference — countries, then cities largest
        first, then regions — so that a name claimed by several places is
        resolved by taking the lowest row number, and no ranking has to be
        stored alongside. A name really can mean several places: there are
        thirty Springfields, and which one is meant is a question for the
        reader, not for this script.
    """
    countries_out: dict = {}
    country_rows, region_rows = [], []

    # The gazetteer's country list comes from 1:50m, not from the outline
    # set: at 1:110m the Maldives, the Seychelles and Malta are too small to
    # draw and are simply absent, and somewhere a person spends a fortnight
    # has to be findable whether or not it can be drawn. A country with no
    # outline is a dot without a filled shape behind it.
    countries = fetch("ne_50m_admin_0_countries")
    by_country_name, by_country_code = {}, {}
    for feature in countries["features"]:
        properties = feature["properties"]
        code = properties["ADM0_A3"]
        if code == "ATA":
            continue
        names = alias_values(
            properties,
            ["NAME", "NAME_LONG", "ADMIN", "BRK_NAME", "FORMAL_EN", "NAME_SORT"]
            + [f"NAME_{language}" for language in NAME_LANGUAGES],
        )
        names += COUNTRY_NICKNAMES.get(code, [])
        countries_out[code] = properties["NAME"]
        by_country_name[properties["NAME"]] = code
        by_country_code[code] = properties["NAME"]
        country_rows.append(
            (
                # LABEL_X/Y is where Natural Earth would print the country's
                # name: a point inside the country and away from its edges,
                # which a centroid is not for Norway or Indonesia.
                [
                    KIND_COUNTRY,
                    properties["NAME"],
                    code,
                    round(float(properties["LABEL_Y"]), 3),
                    round(float(properties["LABEL_X"]), 3),
                ],
                names,
            )
        )

    def country_code_of(properties: dict, name_key: str, code_key: str) -> str:
        """
        The code of the country a region or city sits in.

        The outline set is 1:110m and drops countries too small to draw, so a
        code arriving here may be one no outline will ever be filled for. It
        is still registered under its name: the Maldives are somewhere a
        person goes, and a dot there should not be labelled with a blank.
        """
        code = properties.get(code_key)
        if code in by_country_code:
            return code
        name = properties.get(name_key) or ""
        if name in by_country_name:
            return by_country_name[name]
        if code and name:
            by_country_code[code] = name
            by_country_name[name] = code
            countries_out.setdefault(code, name)
        return code or ""

    # Regions, and the larger regions their names imply. Natural Earth's
    # admin-1 layer is provinces — Firenze, not Toscana — but each province
    # names the region it belongs to and carries the region's names in its
    # own alternates, so what people call a holiday after is recoverable.
    regions = fetch("ne_10m_admin_1_states_provinces")
    provinces, grouped = [], {}
    for feature in regions["features"]:
        properties = feature["properties"]
        latitude, longitude = properties.get("latitude"), properties.get("longitude")
        if latitude is None or longitude is None:
            continue
        code = country_code_of(properties, "admin", "adm0_a3")
        names = alias_values(
            properties,
            ["name", "name_en", "name_alt", "gn_name", "woe_name", "name_local"],
        )
        if not names:
            continue
        # Two levels of grouping, because Natural Earth uses both: a Spanish
        # province names its autonomous community in `region`, and a Scottish
        # council names Scotland in `geonunit`. Neither field is filled in
        # everywhere, and a province can belong to a group under each.
        groups = [
            (code, properties.get(field) or "")
            for field in ("region", "geonunit")
            # A geonunit equal to the country is Italy inside Italy: a group
            # holding every province there is, whose shared names would be
            # every name in the country.
            if properties.get(field) and properties.get(field) != properties["admin"]
        ]
        provinces.append((groups, properties, names, float(latitude), float(longitude)))
        for group in groups:
            grouped.setdefault(group, {"at": [], "names": []})
            grouped[group]["at"].append((float(latitude), float(longitude)))
            # Deduplicated within the province: `name` and `name_en` are
            # usually the same string, and counting that as two provinces
            # would promote every province's own name to its region.
            grouped[group]["names"] += sorted({normalize(name) for name in names})

    # A name carried by two provinces of the same region is a name for the
    # region, not for either province: every province of Toscana lists
    # "Tuscany" among its alternates. Those names are moved up to the region
    # and taken away from the provinces, so that "Tuscany" lands in the
    # middle of Tuscany rather than in whichever province was read first.
    shared = {}
    for group, bundle in grouped.items():
        counted = {}
        for key in bundle["names"]:
            counted[key] = counted.get(key, 0) + 1
        shared[group] = {key for key, count in counted.items() if count > 1}
        shared[group].add(normalize(group[1]))

    for groups, properties, names, latitude, longitude in provinces:
        taken = (
            set().union(*(shared.get(group, set()) for group in groups))
            if groups
            else set()
        )
        own = [name for name in names if normalize(name) not in taken]
        if not own:
            continue
        region_rows.append(
            (
                [
                    KIND_REGION,
                    properties.get("name_en") or properties["name"],
                    groups[0][0]
                    if groups
                    else country_code_of(properties, "admin", "adm0_a3"),
                    round(latitude, 3),
                    round(longitude, 3),
                ],
                own,
            )
        )

    for group, bundle in grouped.items():
        points = bundle["at"]
        region_rows.append(
            (
                [
                    KIND_REGION,
                    group[1],
                    group[0],
                    round(sum(point[0] for point in points) / len(points), 3),
                    round(sum(point[1] for point in points) / len(points), 3),
                ],
                sorted(shared[group]),
            )
        )

    cities = fetch("ne_10m_populated_places")
    sized = []
    for feature in cities["features"]:
        properties = feature["properties"]
        population = int(properties.get("POP_MAX") or 0)
        capital = bool(properties.get("ADM0CAP"))
        notable = bool(properties.get("WORLDCITY") or properties.get("MEGACITY"))
        # Rank 7 is roughly where Natural Earth stops labelling a place on a
        # world map. Keeping everything above it catches the small towns a
        # holiday is named after — Positano, Interlaken — without carrying
        # every parish on earth.
        ranked = (properties.get("RANK_MAX") or 0) >= 7
        if not (capital or notable or ranked or population >= MIN_CITY_POPULATION):
            continue
        keys = ["NAME", "NAMEASCII", "NAMEALT", "NAME_EN"]
        if capital or notable or population >= 500_000:
            keys += [f"NAME_{language}" for language in NAME_LANGUAGES]
        names = alias_values(properties, keys)
        if not names:
            continue
        sized.append(
            (
                capital or population >= MAJOR_CITY_POPULATION,
                population,
                [
                    KIND_CITY,
                    properties.get("NAME_EN") or properties["NAME"],
                    country_code_of(properties, "ADM0NAME", "ADM0_A3"),
                    round(float(properties["LATITUDE"]), 3),
                    round(float(properties["LONGITUDE"]), 3),
                ],
                names,
            )
        )
    # Largest first, so that Paris in France outranks Paris in Texas without
    # the population itself having to be carried into the app.
    sized.sort(key=lambda entry: -entry[1])
    major = [(row, names) for big, _, row, names in sized if big]
    minor = [(row, names) for big, _, row, names in sized if not big]

    # Countries, then cities big enough that their name is the first thing
    # anybody thinks of, then regions, then everywhere else. The middle two
    # are in that order because of Cornwall: a region in England that half a
    # million people visit, and a town of forty thousand in Ontario.
    extra_rows = [
        ([KIND_REGION, name, code, latitude, longitude], [name] + aliases)
        for name, latitude, longitude, code, aliases in EXTRA_PLACES
    ]

    ordered = country_rows + extra_rows + major + region_rows + minor
    index: dict = {}
    for position, (_, names) in enumerate(ordered):
        for name in names:
            for key in index_keys(name):
                index.setdefault(key, [])
                if position not in index[key]:
                    index[key].append(position)

    print(
        f"  {len(country_rows)} countries, {len(major) + len(minor)} cities, "
        f"{len(region_rows)} regions, {len(index)} names",
        file=sys.stderr,
    )
    return {
        "countries": countries_out,
        "places": [row for row, _ in ordered],
        "index": index,
    }


def main() -> None:
    print("world outlines:", file=sys.stderr)
    WORLD_OUT.write_text(json.dumps(build_world(), separators=(",", ":")))
    print("gazetteer:", file=sys.stderr)
    PLACES_OUT.parent.mkdir(exist_ok=True)
    PLACES_OUT.write_text(json.dumps(build_places(), separators=(",", ":")))
    for path in (WORLD_OUT, PLACES_OUT):
        print(
            f"  {path.relative_to(REPO)}  {path.stat().st_size // 1024} KB",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
