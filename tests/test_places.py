"""
Tests for turning a written destination into a point on the map.

The field is free text and always will be, so what these tests are really
about is the shapes people write in it: a country, a town, a town and its
country, two countries at once, a name in the reader's own language rather
than in English, and something that is not a place at all. The last of those
matters as much as the rest — a destination that names nowhere has to be
reported, because a trip silently missing from a map of every trip is a fault
nobody can see.
"""

from app.places import atlas, locate, normalize


def names(destination):
    """Just the place names a destination resolves to, for comparing."""
    return [place.name for place in locate(destination)]


class TestNormalize:
    """The fold that has to agree with the one the data file was built with."""

    def test_case_accents_and_punctuation_go(self):
        assert normalize("Côte d'Ivoire") == "cote d ivoire"

    def test_a_leading_article_goes(self):
        assert normalize("The Netherlands") == normalize("Netherlands")

    def test_the_sharp_s_becomes_a_double_s(self):
        assert normalize("Preußen") == "preussen"


class TestLocate:
    """One destination, and the places it names."""

    def test_a_country(self):
        assert names("Portugal") == ["Portugal"]

    def test_a_city(self):
        assert names("Lisbon") == ["Lisbon"]

    def test_a_city_and_its_country_is_the_city(self):
        # Two dots for one place would be a lie about where the trip was, and
        # the more specific half is the true one.
        assert names("Lisbon, Portugal") == ["Lisbon"]

    def test_a_city_and_its_country_without_the_comma(self):
        assert names("Lisbon Portugal") == ["Lisbon"]

    def test_two_countries_are_two_places(self):
        # A trip really can go to two places, and the map should say so.
        assert names("Portugal & Spain") == ["Portugal", "Spain"]

    def test_a_name_in_another_language(self):
        assert names("Lissabon") == ["Lisbon"]
        assert names("Schweden") == ["Sweden"]
        assert names("Türkei") == ["Turkey"]

    def test_an_umlaut_typed_as_two_letters(self):
        assert names("Muenchen") == names("München") == ["Munich"]

    def test_a_country_outranks_a_town_of_the_same_name(self):
        assert locate("Mexico")[0].kind == "country"

    def test_the_larger_of_two_cities_wins(self):
        assert locate("Paris")[0].country == "France"

    def test_a_region_outranks_a_small_town_of_the_same_name(self):
        # Cornwall in England is somewhere people go on holiday; Cornwall in
        # Ontario is a town of forty thousand.
        assert locate("Cornwall")[0].country == "United Kingdom"

    def test_a_region_no_administrative_dataset_holds(self):
        assert names("Tuscany") == names("Toskana")
        assert names("Mallorca") == ["Mallorca"]

    def test_an_island_country_too_small_to_draw_is_still_findable(self):
        # The outline set is 1:110m and has no Maldives in it at all.
        assert names("Maldives") == ["Maldives"]

    def test_nothing_is_not_a_place(self):
        assert locate("") == []
        assert locate(None) == []

    def test_a_destination_that_names_nowhere(self):
        assert locate("Wanderlust 2024") == []

    def test_a_short_fragment_is_not_reached_for(self):
        # Trimming a phrase down to its last resort would otherwise match
        # two-letter codes inside ordinary words.
        assert locate("in") == []


def trip(name, destination, net=100.0):
    """One overview row, of the shape :func:`atlas` reads."""
    return {
        "id": name.lower(),
        "name": name,
        "destination": destination,
        "start_date": "2024-05-01",
        "end_date": "2024-05-08",
        "summary": {"net": net},
    }


class TestAtlas:
    """Every trip seen from above."""

    def test_trips_to_one_place_share_a_dot(self):
        result = atlas([trip("May", "Lisbon", 200.0), trip("June", "Lisbon", 300.0)])
        assert len(result["points"]) == 1
        assert result["points"][0]["net"] == 500.0
        assert [t["name"] for t in result["points"][0]["trips"]] == ["May", "June"]

    def test_a_trip_to_two_places_is_on_the_map_twice(self):
        result = atlas([trip("Roadtrip", "Portugal & Spain")])
        assert {point["place"] for point in result["points"]} == {"Portugal", "Spain"}

    def test_the_countries_to_shade_come_from_the_places(self):
        result = atlas([trip("City break", "Lisbon")])
        assert result["countries"] == ["PRT"]

    def test_the_biggest_spend_comes_first(self):
        # The label placer is greedy in this order, so the names it manages to
        # fit are the ones worth reading.
        result = atlas([trip("Small", "Rome", 50.0), trip("Big", "Tokyo", 900.0)])
        assert [point["place"] for point in result["points"]] == ["Tokyo", "Rome"]

    def test_a_destination_that_cannot_be_placed_is_named(self):
        result = atlas([trip("Odd one", "Wanderlust")])
        assert result["points"] == []
        assert result["unplaced"] == [{"label": "Wanderlust"}]

    def test_a_trip_with_no_destination_is_named_by_itself(self):
        # Its own name is what the reader has to recognise it by, and the
        # blank destination is the field to go and fill in.
        result = atlas([trip("Weekend away", None)])
        assert result["unplaced"] == [{"label": "Weekend away"}]
