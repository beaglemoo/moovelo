import uuid
from datetime import datetime
from typing import Any

from geoalchemy2 import Geography, Geometry
from sqlalchemy import (
    DDL,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)

# The dialect ARRAY, not sqlalchemy.ARRAY: only this one implements the
# containment operator that tag filtering needs.
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    is_admin: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Session(Base):
    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WahooAccount(Base):
    __tablename__ = "wahoo_accounts"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    access_token: Mapped[str] = mapped_column(String(500))
    refresh_token: Mapped[str] = mapped_column(String(500))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    athlete: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserSettings(Base):
    """Rider settings the ride-time model draws on: weight, flat-road speed,
    and an optional FTP. Absent for a user who has never opened /settings -
    the API serves defaults without inserting a row."""

    __tablename__ = "user_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    weight_kg: Mapped[float] = mapped_column(Float, default=78.0, server_default="78.0")
    flat_speed_kmh: Mapped[float] = mapped_column(Float, default=22.0, server_default="22.0")
    ftp_watts: Mapped[float | None] = mapped_column(Float, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Route(Base):
    __tablename__ = "routes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    preset: Mapped[str] = mapped_column(String(20))
    # Sibling of `preset`, not part of the snapshot: the resolved
    # BicycleCostingOptions bundle when `preset` is "custom", null when a
    # named preset was used instead. Routes carry the resolved options
    # directly rather than a foreign key to custom_presets, so deleting a
    # saved preset never mutates a route that was saved while it was
    # selected.
    costing_options: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # planned = built from waypoints in the app; imported = read from a file.
    # An imported route's waypoints are only its endpoints, so re-routing it
    # would discard the track that was imported.
    source: Mapped[str] = mapped_column(String(16), default="planned", server_default="planned")
    waypoints: Mapped[list[Any]] = mapped_column(JSONB)
    # Full routing snapshot (RouteResponse shape) so saved routes and their
    # exports stay stable across OSM data refreshes.
    legs: Mapped[list[Any]] = mapped_column(JSONB)
    elevation: Mapped[list[Any]] = mapped_column(JSONB)
    distance_m: Mapped[float]
    duration_s: Mapped[float]
    ascent_m: Mapped[float]
    descent_m: Mapped[float]
    # Surface/road-class/use breakdown from Valhalla /trace_attributes.
    # Nullable: routes saved before Phase 7, and any route where the
    # edge_walk match failed, simply have none.
    surface: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Climbs detected from `elevation` by services/climbs.py. NOT NULL,
    # unlike surface: detect_climbs always returns a (possibly empty) list,
    # so there is no "match failed" case that needs a None to fall back on.
    climbs: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    geom: Mapped[Any] = mapped_column(Geometry(geometry_type="LINESTRING", srid=4326))
    # Wahoo sync state: none | queued | pushing | synced | error
    wahoo_status: Mapped[str] = mapped_column(String(10), default="none", server_default="none")
    wahoo_error: Mapped[str | None] = mapped_column(String(500), default=None)
    wahoo_route_id: Mapped[str | None] = mapped_column(String(64), default=None)
    wahoo_pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Public read-only share link token; null = not shared.
    share_token: Mapped[str | None] = mapped_column(String(64), unique=True, default=None)
    # Natural-language summary, generated once (one LLM call, no tools) when
    # the share link is created or rotated - never on the public read path.
    # summary_signature is a hash of the route's own geometry at generation
    # time (services/route_summary.py); a later re-route changes the
    # geometry without touching either column, so the signature no longer
    # matching is what tells the read path to suppress a stale summary.
    summary: Mapped[str | None] = mapped_column(Text, default=None)
    summary_signature: Mapped[str | None] = mapped_column(Text, default=None)
    # Free-form organisation. Tags rather than folders: a route can be both
    # "gravel" and "with the kids".
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    notes: Mapped[str | None] = mapped_column(Text, default=None)
    is_favourite: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("ix_routes_tags", "tags", postgresql_using="gin"),)


class Activity(Base):
    """A ride that happened, as opposed to one that was planned.

    Deliberately not a flavour of Route. A route is an intention: it has a
    preset, costing options, maneuvers, a Wahoo push state, and it can be
    re-routed. An activity is a record: it has a start time, it has no
    maneuvers, and re-routing it would be a lie. Sharing a table would leave
    half the columns null for every row on both sides.
    """

    __tablename__ = "activities"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    # When the ride happened. Nullable because a file can declare itself an
    # activity and still carry no usable timestamps; the library sorts on
    # created_at for those rather than dropping them.
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    # Wall clock start to finish, and that minus the standing around. Both
    # null together when the file had no timestamps - which is not the same
    # as a ride of zero moving time, so neither defaults to 0.
    elapsed_time_s: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    moving_time_s: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    distance_m: Mapped[float]
    ascent_m: Mapped[float]
    descent_m: Mapped[float]
    # Same shape as Route.elevation, so the existing profile chart renders an
    # activity without knowing it is one.
    elevation: Mapped[list[Any]] = mapped_column(JSONB)
    # Climbs detected from `elevation` by services/climbs.py, same shape as
    # Route.climbs. Computed once at import time
    # (services/activities.py:activity_from_track) rather than on every
    # read: unlike a saved Route, an activity never gets re-routed, so
    # there is nothing that could invalidate a stored value the way a
    # planner edit invalidates Route.climbs. Backfilled for every activity
    # that existed before this column did by migration 0018.
    climbs: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    # The trace as recorded, not map-matched. A heatmap of where you rode
    # should show where you rode, and matching is only introduced where
    # coverage genuinely needs it.
    geom: Mapped[Any] = mapped_column(
        Geometry(geometry_type="LINESTRING", srid=4326, spatial_index=False)
    )
    # file = uploaded directly; strava_export = extracted from a bulk export.
    source: Mapped[str] = mapped_column(String(16), default="file", server_default="file")
    # Whatever identifies this ride in its origin - the original filename, or
    # the Strava activity id parsed out of it. Carries the deduplication.
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    # queued | parsing | done | error
    import_status: Mapped[str] = mapped_column(
        String(16), default="queued", server_default="queued"
    )
    import_error: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # When this activity was last map-matched onto OSM way ids for cycle-
    # network coverage (services/way_matching.py). Null for every activity
    # imported before that feature shipped - that is the backfill queue's
    # own work list - and set on both a successful and a failed attempt, so
    # a track Valhalla genuinely cannot place is not retried forever.
    ways_matched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    # The saved route this ride followed - found automatically by
    # services/route_match.py, or set by hand. SET NULL, not CASCADE:
    # deleting the route must not delete the ride that happened, it just
    # loses its link. index=True names it ix_activities_route_id, matching
    # the migration.
    route_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("routes.id", ondelete="SET NULL"), nullable=True, default=None, index=True
    )
    # The Frechet distance (services/route_match.py), in true ground metres,
    # that produced an auto-match. Null for a manual link or no match.
    match_confidence: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    # Set true the moment a rider picks or clears the matched route by hand,
    # so a later auto-match run never overwrites a human decision.
    match_locked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    __table_args__ = (
        # The library's own query: one user's rides, newest first.
        Index("ix_activities_user_started", "user_id", "started_at"),
        # spatial_index=False above and the index named here, matching Place,
        # Poi and CycleWay. Route.geom does not do this, so its ORM metadata
        # and its migration disagree about the index name - a divergence that
        # only stays harmless because nothing queries routes.geom.
        Index("ix_activities_geom", "geom", postgresql_using="gist"),
        # Re-importing an overlapping export adds only what is new. Partial,
        # so the many rows with no natural identifier do not collide.
        Index(
            "ix_activities_source_ref",
            "user_id",
            "source_ref",
            unique=True,
            postgresql_where=text("source_ref IS NOT NULL"),
        ),
    )


# `match_confidence` describes a match to a route, so it must not outlive the
# route it describes. ON DELETE SET NULL is pure DDL - it nulls `route_id` and
# touches nothing else - so a deleted route would otherwise leave its score
# behind, and the endpoints are not the only thing that can null the column.
# Enforcing it here holds for every path: the API, a bulk delete, a psql
# session during an incident.
#
# It deliberately leaves `match_locked` alone. Clearing a link by hand sets
# `route_id = NULL` and `match_locked = true` together - that pairing is how a
# rider says "there is no route for this ride, stop guessing" - so resetting
# the flag whenever `route_id` went null would undo the rider's own decision.
#
# Declared against the table's metadata as well as in migration 0017 because
# the test databases are built by `Base.metadata.create_all`, not by Alembic
# (see tests/conftest.py, which mirrors migrations the same way). The two must
# be kept in step by hand; the SQL text below is the same in both places.
_CLEAR_MATCH_CONFIDENCE_FN = """
CREATE FUNCTION activities_clear_match_confidence() RETURNS trigger AS $$
BEGIN
    IF NEW.route_id IS NULL THEN
        NEW.match_confidence := NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_CLEAR_MATCH_CONFIDENCE_TRIGGER = """
CREATE TRIGGER activities_clear_match_confidence
BEFORE INSERT OR UPDATE OF route_id, match_confidence ON activities
FOR EACH ROW EXECUTE FUNCTION activities_clear_match_confidence()
"""

event.listen(
    Activity.__table__,
    "after_create",
    # DDL's constructor carries no type annotations in sqlalchemy itself
    # (unlike the rest of Core), so this is a genuine gap in the library's
    # own stubs, not a call worth leaving untyped by choice.
    DDL(_CLEAR_MATCH_CONFIDENCE_FN).execute_if(dialect="postgresql"),  # type: ignore[no-untyped-call]
)
event.listen(
    Activity.__table__,
    "after_create",
    DDL(_CLEAR_MATCH_CONFIDENCE_TRIGGER).execute_if(dialect="postgresql"),  # type: ignore[no-untyped-call]
)


# A rider's manual pick records a decision ABOUT A PARTICULAR ROUTE, so it
# cannot outlive that route. Deleting the route nulls `route_id` through the
# FK and used to leave `match_locked` set, which froze the ride: invisible to
# every auto-match pass, indistinguishable in the API and the UI from a ride
# that was simply never matched, and recoverable only by remembering which
# route was deleted and picking a replacement by hand.
#
# It has to be a DELETE trigger on `routes`, and that is the whole point.
# `route_id IS NULL AND match_locked` has two causes - "the rider cleared it"
# and "the route was deleted" - which is why an earlier attempt to fix this
# inside the rematch endpoint could not tell them apart and undid deliberate
# clears. The UPDATE trigger above cannot either; it fires for both. Only the
# deletion itself knows which case it is, so the answer belongs where the
# deletion happens - and then no extra column is needed to distinguish states
# a trigger never has to guess between.
#
# Scoped to `route_id = OLD.id`: a ride the rider cleared by hand has no
# route_id at all, so it can never match here and its "stop guessing" is left
# exactly as docs/architecture.md promises.
_UNLOCK_ORPHANED_MATCH_FN = """
CREATE FUNCTION routes_unlock_orphaned_matches() RETURNS trigger AS $$
BEGIN
    UPDATE activities
       SET match_locked = false
     WHERE route_id = OLD.id
       AND match_locked;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql
"""

_UNLOCK_ORPHANED_MATCH_TRIGGER = """
CREATE TRIGGER routes_unlock_orphaned_matches
BEFORE DELETE ON routes
FOR EACH ROW EXECUTE FUNCTION routes_unlock_orphaned_matches()
"""

event.listen(
    Activity.__table__,
    "after_create",
    DDL(_UNLOCK_ORPHANED_MATCH_FN).execute_if(dialect="postgresql"),  # type: ignore[no-untyped-call]
)
event.listen(
    Activity.__table__,
    "after_create",
    DDL(_UNLOCK_ORPHANED_MATCH_TRIGGER).execute_if(dialect="postgresql"),  # type: ignore[no-untyped-call]
)


class CustomPreset(Base):
    """A user's named costing-slider bundle, applied by name from the
    planner. Not referenced by Route.costing_options - a route stores its
    resolved options directly, so renaming or deleting a preset here never
    changes a route that was saved while it was selected."""

    __tablename__ = "custom_presets"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(60))
    options: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("user_id", "name"),)


# The five tables below are written only by the indexer sidecar, which is a
# separate process on its own compose profile. The app reads them and never
# writes them, so they carry no timestamps and no ownership.


class Place(Base):
    """A named settlement or feature: what place search resolves against."""

    __tablename__ = "places"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    osm_type: Mapped[str] = mapped_column(String(1))
    osm_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(Text)
    # Accent-folded and casefolded by the indexer. The trigram index lives
    # here rather than on name, because unaccent() is STABLE and so cannot
    # be indexed directly.
    name_norm: Mapped[str] = mapped_column(Text)
    place_type: Mapped[str] = mapped_column(Text)
    county: Mapped[str | None] = mapped_column(Text, default=None)
    population: Mapped[int | None] = mapped_column(Integer, default=None)
    # Ranking weight from place_type and population, computed at index time.
    importance: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    geog: Mapped[Any] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False)
    )

    __table_args__ = (
        Index("ix_places_osm", "osm_type", "osm_id", unique=True),
        Index(
            "ix_places_name_trgm",
            "name_norm",
            postgresql_using="gin",
            postgresql_ops={"name_norm": "gin_trgm_ops"},
        ),
        Index("ix_places_geog", "geog", postgresql_using="gist"),
    )


class Poi(Base):
    """Somewhere useful on a ride: water, coffee, a bike shop, a viewpoint."""

    __tablename__ = "pois"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    osm_type: Mapped[str] = mapped_column(String(1))
    osm_id: Mapped[int] = mapped_column(BigInteger)
    # Nullable because the unnamed ones - a drinking fountain, a repair
    # stand - are exactly the POIs worth finding.
    name: Mapped[str | None] = mapped_column(Text, default=None)
    name_norm: Mapped[str | None] = mapped_column(Text, default=None)
    category: Mapped[str] = mapped_column(Text)
    osm_tags: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    geog: Mapped[Any] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False)
    )

    __table_args__ = (
        Index("ix_pois_osm", "osm_type", "osm_id", unique=True),
        Index("ix_pois_geog", "geog", postgresql_using="gist"),
        Index("ix_pois_category", "category"),
        # Partial, so the unnamed majority costs nothing to index.
        Index(
            "ix_pois_name_trgm",
            "name_norm",
            postgresql_using="gin",
            postgresql_ops={"name_norm": "gin_trgm_ops"},
            postgresql_where=text("name_norm IS NOT NULL"),
        ),
    )


class CycleWay(Base):
    """One signed cycle route: an OSM route relation, not a single way.

    Keyed on the relation so the overlay can label a line "NCN 6" rather
    than drawing anonymous segments.
    """

    __tablename__ = "cycle_ways"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    ref: Mapped[str | None] = mapped_column(Text, default=None)
    name: Mapped[str | None] = mapped_column(Text, default=None)
    network: Mapped[str] = mapped_column(Text)
    operator: Mapped[str | None] = mapped_column(Text, default=None)
    geom: Mapped[Any] = mapped_column(
        Geometry(geometry_type="MULTILINESTRING", srid=4326, spatial_index=False)
    )

    __table_args__ = (
        Index("ix_cycle_ways_geom", "geom", postgresql_using="gist"),
        Index("ix_cycle_ways_network", "network"),
    )


class CycleWayMember(Base):
    """One OSM way belonging to one signed cycle route, with its own length.

    Published by the indexer alongside CycleWay, from the same member table
    `assemble_cycle_routes` collapses into CycleWay.geom - and unlike that
    table, members keep their own shape. The merge into one MULTILINESTRING
    per relation is what makes the network overlay *tile* affordable, and
    this table is never tiled, so that constraint does not reach it.

    It has to keep the geometry: filtering coverage through CycleWay.geom
    instead tests the envelope of a whole national route. Measured on the
    England extract, a 12 km box around Tring selected 8,301 member ways
    totalling 1,921 km against the 125 km actually inside it, because NCN
    1's envelope spans most of the country.
    """

    __tablename__ = "cycle_way_members"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    relation_id: Mapped[int] = mapped_column(BigInteger)
    way_id: Mapped[int] = mapped_column(BigInteger)
    length_m: Mapped[float] = mapped_column(Float)
    geom: Mapped[Any] = mapped_column(
        Geometry(geometry_type="LINESTRING", srid=4326, spatial_index=False)
    )

    __table_args__ = (
        Index("ix_cycle_way_members_relation_way", "relation_id", "way_id", unique=True),
        Index("ix_cycle_way_members_way", "way_id"),
        Index("ix_cycle_way_members_geom", "geom", postgresql_using="gist"),
    )


class OsmWay(Base):
    """One OSM highway way a bicycle could ride - the all-roads coverage
    denominator (Phase 10 PR8), distinct from CycleWayMember's signed-network
    denominator: this includes every bikeable road, not just the ones
    carrying a route relation.

    Published by the indexer, one row per way, keyed on the OSM way id
    itself - unlike CycleWayMember, a road way has no owning relation to
    pair it with, so there is nothing else a natural key could be. Geometry
    is simplified at index time (indexer/indexer/db.py:assemble_road_ways);
    nothing here is ever drawn, only summed and bbox-tested, so that
    tolerance is deliberately coarser than anything meant for display.

    See indexer/indexer/categories.py:ROAD_HIGHWAY_EXCLUDE for exactly what
    counts as bikeable - footways, paths, bridleways and tracks are kept on
    purpose, and access=private/no is not filtered at all.
    """

    __tablename__ = "osm_ways"

    way_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    highway: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text, default=None)
    length_m: Mapped[float] = mapped_column(Float)
    geom: Mapped[Any] = mapped_column(
        Geometry(geometry_type="LINESTRING", srid=4326, spatial_index=False)
    )

    __table_args__ = (
        Index("ix_osm_ways_geom", "geom", postgresql_using="gist"),
        Index("ix_osm_ways_highway", "highway"),
    )


class ActivityWay(Base):
    """One OSM way a rider has been recorded riding, from services/way_matching.py.

    App-owned, unlike CycleWayMember above: written as activities are
    map-matched, one row per (user, way) rather than per ride -
    `ride_count` and `first_ridden_at` accumulate across every activity
    that touched it. The composite primary key is the only index this
    needs: coverage always looks a way up by (user_id, way_id) together,
    the matching upsert conflicts on the same pair, and nothing in this
    feature queries by way_id alone across users.
    """

    __tablename__ = "activity_ways"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    way_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    first_ridden_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ride_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class SearchIndexMeta(Base):
    """When the index was last built, and from what.

    A single row, enforced by the type: the app asks "has the index been
    built" and there is only ever one answer.
    """

    __tablename__ = "search_index_meta"

    id: Mapped[bool] = mapped_column(Boolean, primary_key=True, default=True, server_default="true")
    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_files: Mapped[list[str]] = mapped_column(ARRAY(Text))
    place_count: Mapped[int] = mapped_column(Integer)
    poi_count: Mapped[int] = mapped_column(Integer)
    cycle_way_count: Mapped[int] = mapped_column(Integer)
    # Nullable, no default: only a rebuild by an indexer new enough to
    # publish cycle_way_members sets this. NULL is what lets
    # /api/coverage/cycle-network tell an install that has not been
    # re-indexed since this feature shipped apart from one that has never
    # been indexed at all - both would otherwise report a dishonest 0%.
    cycle_way_member_count: Mapped[int | None] = mapped_column(Integer, default=None)
    # Same reasoning as cycle_way_member_count, for the all-roads
    # denominator: nullable with no default, so /api/coverage/roads can
    # tell an index that predates osm_ways apart from one that has simply
    # never been built.
    osm_way_count: Mapped[int | None] = mapped_column(Integer, default=None)
    # Which extract osm_ways itself was last (re)built from - not always the
    # same as source_files above. A rebuild with INDEX_ROADS off leaves the
    # live osm_ways table untouched (indexer/db.py:publish) but still
    # overwrites source_files/built_at with the run that just finished, so
    # without a column of its own there is nothing distinguishing "osm_ways
    # matches the rest of this index" from "osm_ways is a leftover from a
    # previous extract that no longer matches source_files at all". NULL
    # alongside osm_way_count NULL means "never built"; once set, it is only
    # ever rewritten by a rebuild that actually ran with roads on.
    osm_ways_source_files: Mapped[list[str] | None] = mapped_column(ARRAY(Text), default=None)

    __table_args__ = (CheckConstraint("id", name="ck_search_index_meta_singleton"),)


class LLMSettings(Base):
    """Install-wide assistant endpoint configuration, set from /admin.

    A single row, following SearchIndexMeta: the question is "how is this
    install configured" and there is only ever one answer.

    Every column is nullable and NULL means "fall back to the environment
    variable". That keeps a declarative install working untouched - the
    table only exists to let an operator change endpoint, model or routing
    without a redeploy, which matters because provider choice moves both
    cost and latency substantially.

    api_key is stored in plain text, as the OIDC and Wahoo secrets are in
    their env vars. It is never returned by any endpoint.
    """

    __tablename__ = "llm_settings"

    id: Mapped[bool] = mapped_column(Boolean, primary_key=True, default=True, server_default="true")
    base_url: Mapped[str | None] = mapped_column(Text, default=None)
    model: Mapped[str | None] = mapped_column(Text, default=None)
    api_key: Mapped[str | None] = mapped_column(Text, default=None)
    provider_order: Mapped[str | None] = mapped_column(Text, default=None)
    # OpenRouter routing mode: balanced (default), price, throughput, latency.
    # NULL leaves it to the gateway, which measured 2.4x more expensive than
    # any explicit mode on the same model.
    provider_sort: Mapped[str | None] = mapped_column(String(20), default=None)
    # Optional ceiling in dollars per million prompt tokens. The same model
    # can be served by providers an order of magnitude apart in price, so
    # this is the only setting that actually caps spend.
    max_prompt_price: Mapped[float | None] = mapped_column(Float, default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (CheckConstraint("id", name="ck_llm_settings_singleton"),)
