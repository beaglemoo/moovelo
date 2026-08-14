import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

Preset = Literal["road", "gravel", "quiet"]
# What a saved route's `preset` column may hold. "custom" marks a route
# whose costing came from sliders rather than one of the three named
# bundles - it is never a valid PRESETS key, so it is kept out of `Preset`,
# which is passed straight into Valhalla-facing lookups.
StoredPreset = Literal["road", "gravel", "quiet", "custom"]

Latitude = Annotated[float, Field(ge=-90, le=90)]
Longitude = Annotated[float, Field(ge=-180, le=180)]


class BicycleCostingOptions(BaseModel):
    """User-chosen overrides for Valhalla's bicycle costing, one request's
    worth. `extra="forbid"` makes this model the server-side allowlist of
    what a client may tune - validated at request parse, before anything
    reaches Valhalla - and the bounds mirror services/presets.py's own
    three bundles (see backend/tests/test_presets.py)."""

    model_config = ConfigDict(extra="forbid")

    bicycle_type: Literal["Road", "Hybrid", "Cross", "Mountain"]
    cycling_speed: float = Field(ge=10, le=35)
    use_roads: float = Field(ge=0, le=1)
    use_hills: float = Field(ge=0, le=1)
    avoid_bad_surfaces: float = Field(ge=0, le=1)


class AppConfig(BaseModel):
    """What the frontend needs to know before it renders anything."""

    tile_url_cyclosm: str | None = None
    # False until the place index has been built. The indexer is opt-in, so
    # a default install has no places, POIs or cycle routes - and the search
    # box, POI panel and network overlay all stay hidden rather than
    # offering features that would answer nothing.
    search_enabled: bool = False
    # When the index was last built, as epoch seconds. Goes on the cycle
    # network tile URL so a re-index changes the URL: the tiles carry a long
    # max-age, and without this a browser would keep serving yesterday's
    # network for a day after the indexer reran. None when unbuilt.
    search_index_version: str | None = None
    # False unless WEATHER_API_URL is set. Gates the weather panel entirely -
    # nothing reaches outside the LAN unless this is configured.
    weather_enabled: bool = False
    # False unless both LLM_BASE_URL and LLM_MODEL are set. Gates the route
    # assistant panel entirely - no route data leaves the LAN unless this is
    # configured, and then only to the endpoint the operator chose.
    assistant_enabled: bool = False


class PlaceResult(BaseModel):
    id: int
    name: str
    # city / town / village / hamlet / suburb / locality / peak / station
    place_type: str
    lat: float
    lon: float
    # Straight-line metres from the `near` point, when one was given. The
    # frontend shows it because 21,848 of England's places share a name
    # with another, and distance is what tells two Newports apart.
    distance_m: float | None = None


# A real 50 km road route decodes to about 1,800 points, so this leaves
# room for a very long one while keeping the request body bounded.
MAX_ROUTE_POINTS = 20_000
# Further than this and "along the route" stops meaning anything.
MAX_POI_RADIUS_M = 5_000.0
MAX_POI_RESULTS = 300


class PoiQuery(BaseModel):
    """POIs near a route line.

    The line is sent rather than a route id so the planner can ask before
    anything is saved - which is when "where is water on this ride" is
    actually asked.
    """

    # [lon, lat], matching GeoJSON and the decoded polyline the frontend
    # already holds. Bounded like Waypoint is: PostGIS accepts a latitude of
    # 500 without complaint and returns a distance computed from it, so
    # nonsense in comes back as plausible nonsense out rather than a 422.
    line: list[tuple[Longitude, Latitude]] = Field(min_length=2, max_length=MAX_ROUTE_POINTS)
    radius_m: float = Field(default=250.0, gt=0, le=MAX_POI_RADIUS_M)
    # Empty means every category. Unknown names simply match nothing.
    categories: list[str] = Field(default_factory=list, max_length=32)


class PoiResult(BaseModel):
    id: int
    # Null for the unnamed majority - 232 of the 722 POIs within 250 m of a
    # Tring to Oxford ride are unnamed bike parking - so the UI must render
    # the category instead, never an empty row.
    name: str | None
    category: str
    lat: float
    lon: float
    dist_from_route_m: float
    # Metres from the start, measured along the route. What turns a list of
    # nearby POIs into a plan for the ride.
    dist_along_m: float
    # opening_hours, website and the like, straight from OSM. Untrusted
    # text: render it, never interpret it.
    tags: dict[str, str]


class PoisAlongRoute(BaseModel):
    pois: list[PoiResult]
    # True when MAX_POI_RESULTS cut the list short, so the UI can say so
    # rather than quietly presenting a partial answer as a complete one.
    truncated: bool


DEFAULT_WEIGHT_KG = 78.0
DEFAULT_FLAT_SPEED_KMH = 22.0


class UserSettingsResponse(BaseModel):
    weight_kg: float
    flat_speed_kmh: float
    ftp_watts: float | None = None
    # Only ever explicitly set by the /api/settings handlers, which are the
    # sole callers whose response reaches a client. Internal uses of this
    # model (e.g. resolving a rider's settings for ride-time math) never
    # surface it, so the default is never seen outside a bug.
    version: str = "unknown"


class UserSettingsPatch(BaseModel):
    weight_kg: float | None = Field(default=None, ge=30, le=200)
    flat_speed_kmh: float | None = Field(default=None, ge=5, le=60)
    # Nullable *and* optional: omitting the field leaves ftp_watts untouched,
    # sending it as null clears a previously set value. exclude_unset is
    # what tells these two apart. gt=0, not ge=0: a 0 here would zero the
    # rider's speed outright once cubed into effective_flat_speed's scale
    # factor, so it is rejected rather than accepted as a roundabout "no
    # FTP" - that meaning already belongs to the field being unset or null.
    ftp_watts: float | None = Field(default=None, gt=0, le=2000)

    @field_validator("weight_kg", "flat_speed_kmh")
    @classmethod
    def _reject_explicit_null(cls, value: float | None, info: ValidationInfo) -> float | None:
        """Unlike ftp_watts, these two have no "cleared" meaning to fall
        back to - the ride-time model divides by them - so an explicit null
        is a request error rather than a no-op.

        `validate_default` is False by default, so this only runs when the
        field was actually present in the request body: omitting it entirely
        never triggers validation of the (also-None) default, which is the
        whole trick that keeps "omit to leave unchanged" working.
        """
        if value is None:
            raise ValueError(
                f"{info.field_name} cannot be null - omit the field to leave it unchanged"
            )
        return value


class RideTimeSuggestion(BaseModel):
    """GET /api/settings/ride-time-suggestion: a flat_speed_kmh value the
    rider's own matched rides imply, offered - never applied - alongside
    their current setting. Applying it is the existing PATCH /api/settings;
    this endpoint has no write path of its own. Absent (the endpoint
    returns null) below services/ride_calibration.py's floor of usable
    rides."""

    suggested_kmh: float
    sample_size: int
    current_kmh: float


class SurfaceBreakdown(BaseModel):
    """Aggregated Valhalla /trace_attributes edges over a route's own shape.

    Metres, not fractions - summable across chunks, and the frontend derives
    percentages from `total_m`. Keys are Valhalla's own enum strings
    (including ones absent from its documented enum, e.g. "service_road" and
    "parking_aisle"), so these are plain dicts rather than a Literal/enum.
    """

    total_m: float
    surface_m: dict[str, float] = {}
    road_class_m: dict[str, float] = {}
    use_m: dict[str, float] = {}
    # Metres where cycle_lane is present and not "none". Kept separate from
    # use_m: surface mix and marked cycling infrastructure measure different
    # things, and conflating them would misrepresent both.
    cycle_lane_m: float = 0.0


class Waypoint(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class RouteRequest(BaseModel):
    waypoints: list[Waypoint] = Field(min_length=2)
    preset: Preset = "road"
    # When set, overrides `preset` entirely - see services/presets.py's
    # resolve_costing, the one place that decision is made.
    costing_options: BicycleCostingOptions | None = None
    # Points to route around, each snapped to its nearest road which is
    # then excluded from path computation (Valhalla's `exclude_locations`).
    # Capped well under Valhalla's own service limit (11 accepted in
    # testing) - this is a rider-facing "not that road" list, not a bulk
    # avoidance tool.
    exclude_locations: list[Waypoint] | None = Field(default=None, max_length=10)


class ElevationPoint(BaseModel):
    dist_m: float
    elev_m: float


class RideTimePoint(BaseModel):
    """Cumulative elapsed time to a distance along the route, at the
    rider's effective flat speed adjusted for gradient and surface.

    Computed on read from `services/ride_time.py`; never persisted, and
    entirely separate from `duration_s`, which is Valhalla's own estimate
    and drives FIT course-point timing and the Wahoo push payload. Also
    accepted as client input by the weather endpoint (to time wind samples
    to each point's arrival), hence the non-negative bounds.
    """

    dist_m: float = Field(ge=0)
    time_s: float = Field(ge=0)


class RouteLeg(BaseModel):
    # Encoded polyline6 for this leg; maneuvers reference shape indices within it.
    geometry: str
    maneuvers: list[dict[str, Any]]


class Climb(BaseModel):
    """One climb detected by services/climbs.py, ordered by start_dist_m in
    whatever list carries them - detect_climbs already returns them in
    route order, so nothing here re-sorts."""

    start_dist_m: float
    end_dist_m: float
    length_m: float
    gain_m: float
    avg_grade_pct: float
    max_grade_pct: float
    score: float
    # HC, 1, 2, 3 or 4 - HC steepest/longest. A plain str rather than a
    # Literal: CATEGORY_THRESHOLDS is the one place that enumerates them.
    category: str


class RouteResponse(BaseModel):
    legs: list[RouteLeg]
    distance_m: float
    duration_s: float
    ascent_m: float
    descent_m: float
    elevation: list[ElevationPoint]
    # None default is what keeps snapshots stored before Phase 7 - and any
    # route whose edge_walk match failed - parsing without a migration of
    # their own.
    surface: SurfaceBreakdown | None = None
    # Empty default is what keeps snapshots stored before Phase 7's climb
    # detection parsing without a migration of their own - unlike surface,
    # the column itself is NOT NULL DEFAULT '[]', so this default only ever
    # matters for a RouteResponse built by hand (e.g. in a test) rather than
    # for anything actually read back out of the database.
    climbs: list[Climb] = []
    # Computed on read from elevation + surface + the viewer's rider
    # settings - never stored, so it is never part of a saved snapshot and
    # a settings change is reflected immediately on every existing route.
    ride_time: list[RideTimePoint] = []


class RouteSurfaceResponse(BaseModel):
    """Response to POST /api/route/surface: the surface breakdown for the
    given legs and, when elevation was supplied, a ride-time estimate
    computed with that breakdown - one request lets the planner's live
    estimate become surface-aware without a second round trip to
    /api/route, which runs before any surface breakdown exists."""

    surface: SurfaceBreakdown | None
    # [] when elevation was absent or had fewer than two points, matching
    # compute_ride_time's own degenerate-input behaviour.
    ride_time: list[RideTimePoint] = []


class RouteSaveRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    waypoints: list[Waypoint] = Field(min_length=2)
    preset: StoredPreset
    costing_options: BicycleCostingOptions | None = None
    snapshot: RouteResponse
    # Same bound as RoutePatchRequest. Saveable at creation because the
    # library's search advertises "names and notes", and until now there was
    # nowhere in the app to write one.
    notes: str | None = Field(default=None, max_length=5000)

    @model_validator(mode="after")
    def _custom_and_options_travel_together(self) -> "RouteSaveRequest":
        # "custom" is a marker for "the stored costing_options bundle", not a
        # PRESETS key - stored without one, the next re-route (reverse, say)
        # would KeyError inside PRESETS["custom"]. The converse holds too: a
        # NAMED preset stored beside a bundle would let the bundle silently
        # win every future re-route while the preset field claims otherwise
        # (docs/architecture.md documents costing_options as null unless
        # preset is "custom"). The planner always sends a coherent pair;
        # this holds the API to the same invariant in both directions.
        if self.preset == "custom" and self.costing_options is None:
            raise ValueError('preset "custom" requires costing_options.')
        if self.preset != "custom" and self.costing_options is not None:
            raise ValueError('costing_options requires preset "custom".')
        return self


class RoutePatchRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    tags: list[str] | None = Field(default=None, max_length=30)
    notes: str | None = Field(default=None, max_length=5000)
    is_favourite: bool | None = None
    waypoints: list[Waypoint] | None = Field(default=None, min_length=2)
    preset: StoredPreset | None = None
    # Only applied when `preset` is also sent - the planner always resends
    # both together, so this is what lets switching back to a named preset
    # clear a previously stored custom bundle. See api/routes.py:update_route.
    costing_options: BicycleCostingOptions | None = None

    @model_validator(mode="after")
    def _custom_and_options_travel_together(self) -> "RoutePatchRequest":
        # Same two-way invariant as RouteSaveRequest: "custom" without a
        # bundle KeyErrors on the next re-route, and a bundle beside a named
        # preset would silently drive every re-route while the preset field
        # claims otherwise.
        if self.preset == "custom" and self.costing_options is None:
            raise ValueError('preset "custom" requires costing_options.')
        if self.preset is not None and self.preset != "custom" and self.costing_options is not None:
            raise ValueError('costing_options requires preset "custom".')
        return self

    snapshot: RouteResponse | None = None


class WahooState(BaseModel):
    status: str = "none"
    error: str | None = None
    route_id: str | None = None
    pushed_at: datetime | None = None


class RouteSummary(BaseModel):
    id: uuid.UUID
    name: str
    preset: str
    source: str = "planned"
    tags: list[str] = []
    is_favourite: bool = False
    distance_m: float
    ascent_m: float
    updated_at: datetime
    wahoo: WahooState = WahooState()
    share_token: str | None = None

    @classmethod
    def from_route(cls, route: Any) -> "RouteSummary":
        return cls(
            id=route.id,
            name=route.name,
            preset=route.preset,
            source=route.source,
            tags=route.tags,
            is_favourite=route.is_favourite,
            distance_m=route.distance_m,
            ascent_m=route.ascent_m,
            updated_at=route.updated_at,
            wahoo=WahooState(
                status=route.wahoo_status,
                error=route.wahoo_error,
                route_id=route.wahoo_route_id,
                pushed_at=route.wahoo_pushed_at,
            ),
            share_token=route.share_token,
        )


class SavedRoute(RouteResponse):
    id: uuid.UUID
    name: str
    preset: str
    # Echoed back so reloading a route can restore the sliders and the
    # Custom pill - a plain dict, not BicycleCostingOptions, matching how
    # it is stored (services/presets.py's BicycleOptions shape).
    costing_options: dict[str, str | float | int] | None = None
    source: str = "planned"
    tags: list[str] = []
    notes: str | None = None
    is_favourite: bool = False
    waypoints: list[Waypoint]
    updated_at: datetime
    wahoo: WahooState = WahooState()
    share_token: str | None = None


class ArchiveImportStatus(BaseModel):
    """Progress of a bulk archive import. Held in memory, not in the database:
    a job is file bytes plus a counter, and neither survives a restart."""

    id: uuid.UUID
    filename: str
    status: str
    total: int
    imported: int
    failed: int
    # Non-rides named as such by the archive's own manifest - runs, swims,
    # walks. Reported rather than silently dropped.
    skipped: int
    # Rides already imported from an earlier, overlapping export.
    duplicates: int
    error: str | None = None
    problems: list[str] = []


class ActivitySummary(BaseModel):
    """A ride as the library lists it - no geometry, no elevation series."""

    id: uuid.UUID
    name: str
    # Null when the file carried no usable timestamps. The list falls back to
    # created_at for ordering rather than hiding the ride.
    started_at: datetime | None = None
    elapsed_time_s: float | None = None
    moving_time_s: float | None = None
    distance_m: float
    ascent_m: float
    descent_m: float
    source: str
    created_at: datetime
    # The saved route this ride matched to, if any - see
    # services/route_match.py. route_name is carried alongside so the UI can
    # show it without a second call per row.
    route_id: uuid.UUID | None = None
    route_name: str | None = None
    # A 0-1 bidirectional coverage score (services/route_match.py) that
    # produced an auto-match - higher is better. Null for a manual link or
    # no match at all.
    match_confidence: float | None = None
    # True once a rider has picked or cleared the match by hand - the
    # auto-matcher will not touch it again.
    match_locked: bool = False


class ActivityDetail(ActivitySummary):
    """One ride, with the geometry needed to draw it."""

    # polyline6, same encoding the planner already decodes for route legs.
    shape: str
    elevation: list[ElevationPoint] = []


class ClimbAscent(BaseModel):
    """One ride up a clustered climb - see services/climb_log.py."""

    activity_id: uuid.UUID
    started_at: datetime | None = None
    # Estimated from the ride's own moving time (falling back to elapsed
    # time when moving time is unknown) spread proportionally over the
    # climb's share of the ride's total distance - services/climb_log.py's
    # module docstring says plainly why this, not per-point timestamps.
    # None when the ride carries neither.
    time_s: float | None = None


class ClimbLogEntry(BaseModel):
    """One climb the rider has recognisably ridden, possibly several times -
    services/climb_log.py's geometric clustering of every detected Climb
    across every one of their activities. category/length_m/gain_m/
    avg_grade_pct are the earliest ascent's own detect_climbs numbers, not
    an average across ascents: every ride's own elevation sampling and
    smoothing shifts these slightly, and picking one real ascent to
    describe the hill is simpler - and no less honest - than inventing a
    synthetic composite.
    """

    lat: float
    lon: float
    category: str
    length_m: float
    gain_m: float
    avg_grade_pct: float
    ascent_count: int
    # Oldest ascent first - the order services/climb_log.py's own query
    # returns activities in (a.started_at, undated rides last by
    # created_at), preserved rather than re-sorted.
    ascents: list[ClimbAscent]


class ClimbLogResponse(BaseModel):
    climbs: list[ClimbLogEntry]


class ActivityRouteLinkRequest(BaseModel):
    """Body of PUT /api/activities/{id}/route. `route_id` null clears the
    link - both a set and a clear are a human decision, so both lock out
    the auto-matcher the same way."""

    route_id: uuid.UUID | None = None


class RouteActivity(BaseModel):
    """One ride matched to a route, as GET /api/routes/{id}/activities lists
    it. No geometry - the ride detail page (GET /api/activities/{id}) is
    where a rider goes to see the trace itself."""

    id: uuid.UUID
    name: str
    started_at: datetime | None = None
    elapsed_time_s: float | None = None
    moving_time_s: float | None = None
    distance_m: float
    ascent_m: float
    # 0-1 bidirectional coverage score, or null for a manual link - same
    # meaning as ActivitySummary.match_confidence, see route_match.py.
    match_confidence: float | None = None


class RouteActivitiesResponse(BaseModel):
    """GET /api/routes/{id}/activities: the rides matched to one route, for
    planned-vs-actual comparison.

    predicted_time_s is a property of the ROUTE, not of any one ride - the
    same compute_ride_time model api/routes.py already uses for a saved
    route's own `ride_time`, over the viewer's rider settings, with the
    total simply the last RideTimePoint's time_s. It sits once at the top
    level rather than repeated on every row. Null when the route has fewer
    than two elevation points, the same case compute_ride_time itself
    returns [] for.
    """

    predicted_time_s: float | None = None
    activities: list[RouteActivity] = []


class HeatmapAvailability(BaseModel):
    """Whether this rider has anything for the personal heatmap to draw.

    Asked once on planner load so the toggle can be left out entirely
    rather than offered as a control that would only ever fetch empty
    tiles - the same reasoning as AppConfig.search_enabled.
    """

    available: bool


class ActivityMonthStats(BaseModel):
    """One calendar month's totals within a year - see ActivityYearStats."""

    month: int
    count: int
    distance_m: float
    # None when no ride in the bucket carried a moving time - distinct from
    # zero, and rendered as "-" rather than "0m". See the endpoint's own
    # comment on why this one is not coalesced.
    moving_time_s: float | None
    ascent_m: float


class ActivityYearStats(BaseModel):
    """One year's riding totals, as GET /api/activities/stats groups them.

    `year` is None for the "undated" bucket - rides whose started_at is
    NULL, which is a real and expected state (see Activity.started_at's own
    comment) and not one this endpoint drops. Those rides have nothing to
    group by month either, so `months` is always empty for that bucket.
    """

    year: int | None
    count: int
    distance_m: float
    # None, not zero, when nothing in the bucket carried a moving time - see
    # ActivityMonthStats.moving_time_s.
    moving_time_s: float | None
    ascent_m: float
    months: list[ActivityMonthStats] = []


class ActivityStatsResponse(BaseModel):
    """Newest year first, the undated bucket (if any) last."""

    years: list[ActivityYearStats]


class NetworkCoverage(BaseModel):
    """Ridden vs total metres of one network tier (icn/ncn/rcn/lcn) within a
    bbox."""

    network: str
    ridden_m: float
    total_m: float


class CoverageResponse(BaseModel):
    """How much of the signed cycle network a rider has ridden, near
    wherever their own activities are.

    `available` is False whenever the answer would be misleading rather
    than merely absent: no place index built yet, one built before this
    feature added member-way lengths
    (search_index_meta.cycle_way_member_count is null), or the rider has
    imported nothing to centre a default bbox on. Reporting 0% in any of
    those cases reads as "you have ridden nothing", which is not the claim
    being made - `reason` says which one it actually is.
    """

    available: bool
    reason: str | None = None
    networks: list[NetworkCoverage] = []


class CoverageBackfillStatus(BaseModel):
    """Progress of map-matching a rider's own activities onto OSM way ids.
    Mirrors ArchiveImportStatus's shape - held in memory, not the database,
    so a restart reports the job as interrupted rather than resuming it."""

    id: uuid.UUID
    status: str
    total: int
    matched: int
    unmatched: int
    error: str | None = None


class HighwayCoverage(BaseModel):
    """Ridden vs total metres of one OSM `highway` class (residential,
    footway, track, ...) within a bbox."""

    highway: str
    ridden_m: float
    total_m: float


class RoadCoverageResponse(BaseModel):
    """How much of every bikeable road a rider has ridden, near wherever
    their own activities are - the wider denominator: every way in
    `osm_ways`, not only the signed cycle network CoverageResponse reports
    on.

    `available` is False whenever the answer would be misleading rather
    than merely absent: the same three reasons CoverageResponse's own
    docstring gives (no place index built yet, one built before this
    feature added osm_ways - search_index_meta.osm_way_count is null - or
    the rider has imported nothing to centre a default bbox on), plus one
    more specific to this table: osm_ways was last (re)built from a
    different extract than the rest of the index
    (search_index_meta.osm_ways_source_files != source_files - see
    migration 0016), which a roads-off rebuild against a new extract leaves
    behind without anything else recording it happened.
    """

    available: bool
    reason: str | None = None
    highways: list[HighwayCoverage] = []


class SharedRoute(RouteResponse):
    """Public view of a shared route - no ids, no owner information."""

    name: str
    preset: str
    updated_at: datetime
    # Stored text only - never generated on this read path. None whenever
    # the assistant was not configured at share time, or the route has been
    # re-routed since (its signature no longer matches the geometry).
    summary: str | None = None


class WeatherQuery(BaseModel):
    """A route line to sample for wind, sent rather than a route id so the
    planner can ask before anything is saved - matching RouteSurfaceQuery
    and PoiQuery."""

    # [lon, lat], matching GeoJSON and RouteSurfaceQuery.line.
    line: list[tuple[Longitude, Latitude]] = Field(min_length=2, max_length=MAX_ROUTE_POINTS)
    start_time: datetime
    # Optional {dist_m, time_s} profile (e.g. from the elevation-aware
    # ride-time model); falls back to a proportional estimate from
    # duration_s when absent.
    ride_time: list[RideTimePoint] | None = None
    duration_s: float | None = Field(default=None, ge=0)

    @field_validator("start_time")
    @classmethod
    def _start_time_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("start_time must include a timezone offset")
        return value


class WindSegment(BaseModel):
    lat: float
    lon: float
    dist_along_m: float
    arrival_iso: str
    wind_speed_ms: float
    wind_direction_deg: float
    # Positive slows you down, negative helps - a plain tailwind reads as a
    # negative number rather than a separate sign flag.
    headwind_ms: float
    crosswind_ms: float


class WeatherAlongRoute(BaseModel):
    segments: list[WindSegment]
    # True when the start time fell outside the forecast provider's window,
    # or the request otherwise degraded rather than raising - the planner
    # says so instead of presenting an empty result as "no wind".
    truncated: bool


class CustomPresetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    options: BicycleCostingOptions

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, value: Any) -> Any:
        # Stripped before the length bounds are checked, so a name that is
        # only whitespace fails min_length rather than saving as blank.
        return value.strip() if isinstance(value, str) else value


class CustomPresetPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    options: BicycleCostingOptions | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class CustomPresetResponse(BaseModel):
    id: uuid.UUID
    name: str
    options: BicycleCostingOptions


class IsochroneContour(BaseModel):
    """One reachability ring: either a time or a distance contour, never
    both - Valhalla's own `/isochrone` takes a list of contours each keyed
    by exactly one of `time` (minutes) or `distance` (km)."""

    # 120 is Valhalla's own service_limits.isochrone.max_time_contour -
    # anything above it always fails downstream, so it is rejected here with
    # a clear message instead.
    # The 5-minute floor matches the input the UI offers and what
    # docs/guide.md has always promised. `min="5"` on a number input marks
    # the field invalid without blocking anything - the bound value goes
    # straight to the request - so the floor was documented but reachable
    # right down to a fraction of a minute, which draws a meaningless blob.
    minutes: float | None = Field(default=None, ge=5, le=120)
    # Distance contours keep the looser bound: nothing in the UI or the docs
    # offers them, so there is no claimed floor here to make true.
    km: float | None = Field(default=None, gt=0, le=200)
    # Valhalla wants this sans "#"; the frontend strips it before sending.
    color: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{6}$")

    @model_validator(mode="after")
    def _exactly_one_of_minutes_or_km(self) -> "IsochroneContour":
        if (self.minutes is None) == (self.km is None):
            raise ValueError("Set exactly one of minutes or km.")
        return self


class IsochroneQuery(BaseModel):
    origin: Waypoint
    # Valhalla's own limit on contours per request.
    contours: list[IsochroneContour] = Field(min_length=1, max_length=4)
    preset: Preset = "road"
    # Overrides `preset` entirely when set - see resolve_costing.
    costing_options: BicycleCostingOptions | None = None
    polygons: bool = True
    denoise: float = Field(default=0.25, ge=0, le=1)
    generalize: float | None = Field(default=None, ge=0)


class IsochroneResponse(BaseModel):
    """Valhalla's own GeoJSON, passed through untouched.

    `extra="allow"` plus only the two fields MapLibre actually needs
    (`type`, `features`) means whatever else Valhalla's isochrone response
    carries - it varies by version - reaches the frontend unmolested rather
    than being silently stripped by a model that tried to fully describe it.
    """

    model_config = ConfigDict(extra="allow")

    type: str
    features: list[dict[str, Any]]


class LoopQuery(BaseModel):
    """Query for the "N km loop from here" generator - see services/loop.py."""

    origin: Waypoint
    # 5 km floor for the same reason as IsochroneContour.minutes: the UI and
    # docs/guide.md both say 5 to 200, and only the HTML input said so before.
    # Below it the bearing search is looking for a loop shorter than the
    # spacing between its own candidate waypoints.
    target_km: float = Field(ge=5, le=200)
    preset: Preset = "road"
    # Overrides `preset` entirely when set - see resolve_costing.
    costing_options: BicycleCostingOptions | None = None
    # The rider's avoids apply here too: a loop is adopted as the working
    # route, so one generated through a road they had already excluded lands
    # as a route that quietly ignores a constraint still shown as active.
    exclude_locations: list[Waypoint] | None = Field(default=None, max_length=10)


class LoopCandidate(BaseModel):
    """One candidate loop: an ordinary out-and-back route through a single
    via point, with the full RouteResponse snapshot already computed - the
    roadmap's own rule that an assistant's (or here, a generator's) proposal
    lands as ordinary editable waypoints, never a second round trip to plan
    it again once picked."""

    waypoints: list[Waypoint]
    snapshot: RouteResponse
    distance_error_km: float
    score: float


class LoopCandidatesResponse(BaseModel):
    candidates: list[LoopCandidate]


class AlternatesQuery(BaseModel):
    """Query for POST /api/route/alternates.

    Bounded to exactly 2 waypoints: Valhalla's `alternates` option is
    documented (and, in practice, only reliable) for a single origin/
    destination pair - a multipoint route has no well-defined "alternative"
    per leg. Rejecting anything else here produces a 422 before the request
    ever reaches Valhalla, rather than a confusing partial or ignored result
    from it.
    """

    waypoints: list[Waypoint] = Field(min_length=2, max_length=2)
    preset: Preset = "road"
    # Overrides `preset` entirely when set - see resolve_costing.
    costing_options: BicycleCostingOptions | None = None
    # The planner's avoids apply to alternates exactly as they do to the
    # primary route - alternates that route straight through an avoided
    # road would be adoptable lies. Same cap as RouteRequest.
    exclude_locations: list[Waypoint] | None = Field(default=None, max_length=10)
    count: int = Field(default=2, ge=1, le=3)


class AlternatesResponse(BaseModel):
    primary: RouteResponse
    alternates: list[RouteResponse]
