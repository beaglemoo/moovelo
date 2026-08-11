"""Extraction against a hand-written stand-in for a filtered extract.

These cover the parts that only real data would otherwise catch: way
centroids, superroute flattening, and objects the extract has no geometry
for. The `osmium tags-filter` pre-pass is not exercised here - it needs the
real binary and a real pbf, and is covered by running the indexer on
staging.
"""

from pathlib import Path

import pytest

from indexer.categories import (
    ROAD_HIGHWAY_EXCLUDE,
    cycle_network,
    filter_expressions,
    importance,
    parse_population,
    road_highway,
)
from indexer.extract import (
    CycleMemberRow,
    PlaceRow,
    PoiRow,
    RoadWayRow,
    read_cycle_routes,
    read_features,
)
from indexer.geometry import midpoint, normalise, polygon_centroid

FIXTURE = Path(__file__).parent / "fixtures" / "tring.osm.xml"


@pytest.fixture(scope="module")
def rows() -> list[PlaceRow | PoiRow | CycleMemberRow | RoadWayRow]:
    routes = read_cycle_routes(FIXTURE)
    return list(read_features(FIXTURE, routes))


def places(
    rows: list[PlaceRow | PoiRow | CycleMemberRow | RoadWayRow],
) -> dict[tuple[str, str], PlaceRow]:
    """Keyed by name *and* type: the fixture has both a town and a railway
    station called Tring, which is exactly the ambiguity search must cope
    with, so keying on name alone would silently drop one."""
    return {(r.name, r.place_type): r for r in rows if isinstance(r, PlaceRow)}


def pois(rows: list[PlaceRow | PoiRow | CycleMemberRow | RoadWayRow]) -> list[PoiRow]:
    return [r for r in rows if isinstance(r, PoiRow)]


def members(
    rows: list[PlaceRow | PoiRow | CycleMemberRow | RoadWayRow],
) -> list[CycleMemberRow]:
    return [r for r in rows if isinstance(r, CycleMemberRow)]


def road_ways(rows: list[PlaceRow | PoiRow | CycleMemberRow | RoadWayRow]) -> list[RoadWayRow]:
    return [r for r in rows if isinstance(r, RoadWayRow)]


def test_settlements_peaks_and_stations_are_all_places(rows: list[object]) -> None:
    found = places(rows)  # type: ignore[arg-type]

    assert ("Tring", "town") in found
    assert ("Tring Station", "hamlet") in found
    assert ("Ivinghoe Beacon", "peak") in found
    # A railway station is not a place=* value, but people search for it.
    assert ("Tring", "station") in found


def test_a_place_with_no_name_is_not_indexed(rows: list[object]) -> None:
    """There is nothing to search for, so the row would only be noise."""
    assert all(p.name for p in places(rows).values())  # type: ignore[arg-type]


def test_the_town_outranks_the_hamlet_of_the_same_name(rows: list[object]) -> None:
    found = places(rows)  # type: ignore[arg-type]

    assert found[("Tring", "town")].importance > found[("Tring Station", "hamlet")].importance
    # ...and above the station that shares its name exactly.
    assert found[("Tring", "town")].importance > found[("Tring", "station")].importance


def test_population_is_parsed_out_of_free_text(rows: list[object]) -> None:
    """OSM writes "11,730", not 11730."""
    assert places(rows)[("Tring", "town")].population == 11730  # type: ignore[arg-type]


def test_accented_names_are_folded_for_matching(rows: list[object]) -> None:
    found = places(rows)  # type: ignore[arg-type]

    assert found[("Ynys Môn", "village")].name == "Ynys Môn"
    assert found[("Ynys Môn", "village")].name_norm == "ynys mon"


def test_an_unnamed_poi_is_kept_but_has_no_search_text(rows: list[object]) -> None:
    water = [p for p in pois(rows) if p.category == "water"]  # type: ignore[arg-type]

    assert len(water) == 1
    assert water[0].name is None
    # Null rather than empty, so the partial trigram index skips it.
    assert water[0].name_norm is None


def test_only_useful_tags_are_carried_over(rows: list[object]) -> None:
    cafe = next(p for p in pois(rows) if p.name == "Cafe Verde")  # type: ignore[arg-type]

    assert cafe.tags["opening_hours"] == "Mo-Sa 08:00-17:00"
    assert cafe.tags["cuisine"] == "coffee_shop"
    assert "fax" not in cafe.tags


def test_a_supermarket_mapped_as_a_building_becomes_its_centroid(rows: list[object]) -> None:
    market = next(p for p in pois(rows) if p.name == "Tring Market")  # type: ignore[arg-type]

    # Inside the L-shaped outline, which spans 51.792-51.794 by -0.660 to
    # -0.656.
    assert 51.792 < market.lat < 51.794
    assert -0.660 < market.lon < -0.656


def test_a_way_the_extract_lacks_nodes_for_is_skipped_not_fatal(rows: list[object]) -> None:
    """A way clipped by the extract boundary keeps its node references but
    not the nodes. Reading a coordinate off one raises, so this is the
    case that would otherwise abort a whole national build."""
    assert not [p for p in pois(rows) if p.name == "Edge Cafe"]  # type: ignore[arg-type]
    # ...and everything after it still came through.
    assert [p for p in pois(rows) if p.name == "Tring Cycles"]  # type: ignore[arg-type]


def test_a_superroute_collects_the_ways_of_its_child_relation() -> None:
    routes = read_cycle_routes(FIXTURE)

    # Route 6 owns way 50 directly and way 51 only via child relation 61.
    assert routes[60].way_ids == {50, 51}
    assert routes[61].way_ids == {51}


def test_a_walking_route_is_not_a_cycle_route() -> None:
    assert 62 not in read_cycle_routes(FIXTURE)


def test_a_way_in_two_routes_is_emitted_for_each(rows: list[object]) -> None:
    """Way 51 belongs to both the superroute and its child."""
    for_way_51 = [m for m in members(rows) if m.way_id == 51]  # type: ignore[arg-type]

    assert sorted(m.relation_id for m in for_way_51) == [60, 61]


def test_route_members_carry_the_relation_identity(rows: list[object]) -> None:
    member = next(m for m in members(rows) if m.relation_id == 60)  # type: ignore[arg-type]

    assert member.network == "ncn"
    assert member.ref == "6"
    assert member.name == "National Route 6"
    assert member.wkt.startswith("LINESTRING(")


def test_the_filter_covers_every_indexed_tag() -> None:
    """The filter is derived from the same tables the extractor reads, so
    a category cannot be added without the filter keeping it."""
    expressions = filter_expressions()

    assert "r/route=bicycle" in expressions
    assert any(e.startswith("nw/amenity=") and "drinking_water" in e for e in expressions)
    assert any(e.startswith("n/place=") and "hamlet" in e for e in expressions)
    assert any(e.startswith("w/highway!=") and "motorway" in e for e in expressions)


def test_the_road_filter_is_the_only_thing_INDEX_ROADS_turns_off() -> None:
    """Off by default, and off means osmium is never asked for the ways.

    That single expression is where the whole cost of all-roads coverage
    sits: measured on England it takes the filtered extract from 45 MB to
    469 MB and the rebuild from 37 seconds to nearly eight minutes. An
    install that only wants place search, POIs, the cycle overlay or
    cycle-network coverage should not pay it - and cycle-route member ways
    still arrive either way, as members of the r/route=bicycle relations.
    """
    without = filter_expressions(include_roads=False)
    with_roads = filter_expressions(include_roads=True)

    assert not any(e.startswith("w/highway!=") for e in without)
    assert "r/route=bicycle" in without, "cycle routes are not what this switches off"
    assert any(e.startswith("nw/amenity=") for e in without), "nor are POIs"
    # Exactly one expression separates them.
    assert len(with_roads) == len(without) + 1


def test_a_residential_street_and_a_footway_are_road_ways(rows: list[object]) -> None:
    found = {r.way_id: r for r in road_ways(rows)}  # type: ignore[arg-type]

    assert found[70].highway == "residential"
    assert found[70].name == "Church Street"
    # People ride footways; dropping them would make the denominator lie.
    assert 71 in found
    assert found[71].name is None


def test_a_motorway_is_not_a_road_way(rows: list[object]) -> None:
    """A bike cannot ride it, so it does not belong in the denominator."""
    ids = {r.way_id for r in road_ways(rows)}  # type: ignore[arg-type]

    assert 72 not in ids


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"highway": "residential"}, "residential"),
        ({"highway": "footway"}, "footway"),
        ({"highway": "path"}, "path"),
        ({"highway": "bridleway"}, "bridleway"),
        ({"highway": "track"}, "track"),
        ({"highway": "cycleway"}, "cycleway"),
        ({"highway": "motorway"}, None),
        ({"highway": "motorway_link"}, None),
        ({"highway": "proposed"}, None),
        ({"highway": "construction"}, None),
        ({"highway": "raceway"}, None),
        ({}, None),
        # access=private/no is deliberately not filtered - see
        # ROAD_HIGHWAY_EXCLUDE's own docstring for why.
        ({"highway": "residential", "access": "private"}, "residential"),
        ({"highway": "track", "access": "no"}, "track"),
    ],
)
def test_road_highway_excludes_only_the_unrideable_classes(
    tags: dict[str, str], expected: str | None
) -> None:
    assert road_highway(tags) == expected


def test_road_highway_exclude_list_is_exactly_whats_documented() -> None:
    assert set(ROAD_HIGHWAY_EXCLUDE) == {
        "motorway",
        "motorway_link",
        "proposed",
        "construction",
        "raceway",
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("11,730", 11730),
        ("~800", 800),
        ("1 234", 1234),
        ("", None),
        ("unknown", None),
        ("0", None),
        ("999999999", None),
    ],
)
def test_population_parsing(raw: str, expected: int | None) -> None:
    assert parse_population(raw) == expected


def test_importance_uses_population_only_to_break_ties_within_a_type() -> None:
    big_village = importance("village", 5000)
    small_town = importance("town", 300)

    # A town is still a town, however small.
    assert small_town > big_village


def test_cycle_network_ignores_unsigned_routes() -> None:
    assert cycle_network({"type": "route", "route": "bicycle", "network": "ncn"}) == "ncn"
    assert cycle_network({"type": "route", "route": "bicycle"}) is None
    assert cycle_network({"type": "route", "route": "foot", "network": "nwn"}) is None


def test_centroid_of_a_concave_ring_is_not_the_vertex_mean() -> None:
    """The case that makes the signed-area formula worth writing."""
    ring = [(0.0, 0.0), (0.0, 4.0), (1.0, 4.0), (1.0, 1.0), (4.0, 1.0), (4.0, 0.0), (0.0, 0.0)]

    centroid = polygon_centroid(ring)

    assert centroid is not None
    mean = (sum(p[0] for p in ring[:-1]) / 6, sum(p[1] for p in ring[:-1]) / 6)
    assert abs(centroid[0] - mean[0]) > 0.05 or abs(centroid[1] - mean[1]) > 0.05


def test_a_degenerate_ring_does_not_divide_by_zero() -> None:
    """Every vertex identical, which real data does contain."""
    assert polygon_centroid([(1.0, 2.0)] * 4) == (1.0, 2.0)


def test_midpoint_follows_length_not_vertex_count() -> None:
    """Vertices bunched at one end must not drag the midpoint with them."""
    line = [(0.0, 0.0), (0.0, 0.1), (0.0, 0.2), (0.0, 10.0)]

    assert midpoint(line) == (0.0, 0.2)


def test_normalise_is_stable_for_plain_ascii() -> None:
    assert normalise("Tring") == "tring"
    assert normalise("  Ivinghoe Beacon ") == "ivinghoe beacon"
