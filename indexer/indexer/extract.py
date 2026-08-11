"""Reading places, POIs and cycle routes out of a filtered extract.

Two passes, for one specific reason. A pbf stores nodes, then ways, then
relations, so a single pass already sees every member way before the
relation that owns it. What it does not handle is a route relation whose
members include *other relations* - superroutes like the National Cycle
Network's long-distance routes, and directional sub-relations - because a
child relation is not guaranteed to appear before its parent. Pass A reads
relations only and resolves that; pass B does the geometry.
"""

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import osmium

from indexer.categories import (
    KEPT_POI_TAGS,
    cycle_network,
    importance,
    parse_population,
    place_type,
    poi_category,
    road_highway,
)
from indexer.geometry import Point, normalise, way_point

log = logging.getLogger(__name__)

# Superroutes nest a few levels at most; this bounds the expansion against
# a relation cycle, which OSM data does occasionally contain.
MAX_RELATION_DEPTH = 5


@dataclass(frozen=True)
class PlaceRow:
    osm_type: str
    osm_id: int
    name: str
    name_norm: str
    place_type: str
    population: int | None
    importance: float
    lat: float
    lon: float


@dataclass(frozen=True)
class PoiRow:
    osm_type: str
    osm_id: int
    name: str | None
    name_norm: str | None
    category: str
    tags: dict[str, str]
    lat: float
    lon: float


@dataclass(frozen=True)
class CycleMemberRow:
    """One way belonging to one route relation, assembled into a route in SQL."""

    relation_id: int
    ref: str | None
    name: str | None
    network: str
    operator: str | None
    way_id: int
    wkt: str


@dataclass(frozen=True)
class RoadWayRow:
    """One OSM way a bicycle could ride, for the all-roads coverage
    denominator - distinct from CycleMemberRow, which only carries ways
    belonging to a *signed* cycle route. Keyed on way_id alone
    (models.OsmWay's primary key): unlike a cycle-route member, a road way
    has no owning relation to be confused with, so there is nothing to
    pair it with the way CycleMemberRow pairs (relation_id, way_id).
    """

    way_id: int
    highway: str
    name: str | None
    wkt: str


@dataclass
class CycleRoute:
    relation_id: int
    ref: str | None
    name: str | None
    network: str
    operator: str | None
    way_ids: set[int] = field(default_factory=set)
    child_relations: set[int] = field(default_factory=set)


@dataclass
class Counts:
    places: int = 0
    pois: int = 0
    cycle_members: int = 0
    road_ways: int = 0
    skipped_no_location: int = 0
    skipped_multipolygon_pois: int = 0


def read_cycle_routes(path: Path) -> dict[int, CycleRoute]:
    """Pass A: which ways belong to which signed cycle route.

    Relations only, so this never touches a node and costs almost nothing.
    """
    routes: dict[int, CycleRoute] = {}
    for obj in osmium.FileProcessor(str(path), osmium.osm.RELATION):
        if not isinstance(obj, osmium.osm.Relation):  # pragma: no cover - entity filtered
            continue
        tags = dict(obj.tags)
        network = cycle_network(tags)
        if network is None:
            continue
        route = CycleRoute(
            relation_id=obj.id,
            ref=tags.get("ref"),
            name=tags.get("name"),
            network=network,
            operator=tags.get("operator"),
        )
        for member in obj.members:
            if member.type == "w":
                route.way_ids.add(member.ref)
            elif member.type == "r":
                route.child_relations.add(member.ref)
        routes[obj.id] = route

    _flatten_superroutes(routes)
    log.info("pass A: %d cycle routes", len(routes))
    return routes


def _flatten_superroutes(routes: dict[int, CycleRoute]) -> None:
    """Pull each child relation's ways up into its parent.

    Repeated to a fixed point rather than recursively, so a relation that
    references itself - directly or through a loop - cannot hang the build.
    """
    for _ in range(MAX_RELATION_DEPTH):
        changed = False
        for route in routes.values():
            for child_id in route.child_relations:
                child = routes.get(child_id)
                if child is None:
                    continue
                new_ways = child.way_ids - route.way_ids
                if new_ways:
                    route.way_ids |= new_ways
                    changed = True
        if not changed:
            return
    log.warning("cycle route nesting deeper than %d levels; truncated", MAX_RELATION_DEPTH)


def read_features(
    path: Path, routes: dict[int, CycleRoute]
) -> Iterator[PlaceRow | PoiRow | CycleMemberRow | RoadWayRow]:
    """Pass B: everything with a location.

    Yields rows rather than accumulating them, so a national extract never
    has to fit in memory on the way to the database.
    """
    way_owners = _way_owners(routes)
    counts = Counts()

    for obj in osmium.FileProcessor(str(path)).with_locations():
        tags = dict(obj.tags)

        if isinstance(obj, osmium.osm.Node):
            row = _node_row(obj, tags)
            if isinstance(row, PlaceRow):
                counts.places += 1
                yield row
            elif isinstance(row, PoiRow):
                counts.pois += 1
                yield row
            continue

        if not isinstance(obj, osmium.osm.Way):
            continue

        points = _way_points(obj, counts)
        if points is None:
            continue

        category = poi_category(tags)
        if category is not None:
            centre = way_point(points, obj.is_closed())
            if centre is not None:
                counts.pois += 1
                yield _poi_row("w", obj.id, tags, category, centre)

        # A single-point way is not a line, and PostGIS rejects the WKT.
        if len(points) < 2:
            continue

        highway = road_highway(tags)
        if highway is not None:
            counts.road_ways += 1
            yield RoadWayRow(
                way_id=obj.id,
                highway=highway,
                name=tags.get("name"),
                wkt=_linestring_wkt(points),
            )

        for relation_id in way_owners.get(obj.id, ()):
            route = routes[relation_id]
            counts.cycle_members += 1
            yield CycleMemberRow(
                relation_id=relation_id,
                ref=route.ref,
                name=route.name,
                network=route.network,
                operator=route.operator,
                way_id=obj.id,
                wkt=_linestring_wkt(points),
            )

    log.info(
        "pass B: %d places, %d POIs, %d route members, %d road ways "
        "(%d ways without geometry, %d multipolygon POIs skipped)",
        counts.places,
        counts.pois,
        counts.cycle_members,
        counts.road_ways,
        counts.skipped_no_location,
        counts.skipped_multipolygon_pois,
    )


def _way_owners(routes: dict[int, CycleRoute]) -> dict[int, list[int]]:
    """way id -> the routes it belongs to. A way can carry several."""
    owners: dict[int, list[int]] = {}
    for route in routes.values():
        for way_id in route.way_ids:
            owners.setdefault(way_id, []).append(route.relation_id)
    return owners


def _way_points(obj: osmium.osm.Way, counts: Counts) -> list[Point] | None:
    """Coordinates for a way, or None if the extract does not have them.

    A way clipped by the extract boundary keeps its node references but not
    the nodes themselves, and reading .lat on one of those raises. Checking
    validity first is what stops a single boundary way aborting the whole
    build.
    """
    points: list[Point] = []
    for node in obj.nodes:
        if not node.location.valid():
            counts.skipped_no_location += 1
            return None
        points.append((node.location.lat, node.location.lon))
    return points or None


def _node_row(obj: osmium.osm.Node, tags: dict[str, str]) -> PlaceRow | PoiRow | None:
    if not obj.location.valid():
        return None
    point = (obj.location.lat, obj.location.lon)

    kind = place_type(tags)
    name = tags.get("name")
    if kind is not None and name:
        population = parse_population(tags.get("population"))
        return PlaceRow(
            osm_type="n",
            osm_id=obj.id,
            name=name,
            name_norm=normalise(name),
            place_type=kind,
            population=population,
            importance=importance(kind, population),
            lat=point[0],
            lon=point[1],
        )

    category = poi_category(tags)
    if category is not None:
        return _poi_row("n", obj.id, tags, category, point)
    return None


def _poi_row(
    osm_type: str, osm_id: int, tags: dict[str, str], category: str, point: Point
) -> PoiRow:
    name = tags.get("name")
    return PoiRow(
        osm_type=osm_type,
        osm_id=osm_id,
        name=name,
        # Left null for the unnamed majority - a drinking fountain, a
        # repair stand - so the partial trigram index stays small.
        name_norm=normalise(name) if name else None,
        category=category,
        tags={key: tags[key] for key in KEPT_POI_TAGS if key in tags},
        lat=point[0],
        lon=point[1],
    )


def _linestring_wkt(points: list[Point]) -> str:
    coordinates = ", ".join(f"{lon} {lat}" for lat, lon in points)
    return f"LINESTRING({coordinates})"
