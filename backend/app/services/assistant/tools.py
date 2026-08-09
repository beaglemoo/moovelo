"""The six tools the assistant may call.

Each is a pydantic argument model plus a handler that calls the existing
service layer directly - no HTTP self-calls. Nothing here routes, searches
or scores anything new; the model only chooses which primitive to use.

Two rules hold throughout:

- No tool accepts a coordinate. Points arrive as handle references, and
  `extra="forbid"` means a stray `lat` key is a validation error rather
  than something to notice later.
- No tool result carries a coordinate, and none carries raw OpenStreetMap
  tags. `PoiResult.tags` is an arbitrary public-authored key/value dict and
  is the largest untrusted surface in the system; it never reaches the
  model at all.
"""

from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas import (
    BicycleCostingOptions,
    Preset,
    RouteRequest,
    RouteResponse,
    Waypoint,
)
from app.services.assistant.refs import HandleTable, UnknownHandleError
from app.services.geo import concat_shapes
from app.services.loop import generate_loop_candidates
from app.services.places import pois_along_route, search_places
from app.services.polyline import decode_polyline6
from app.services.valhalla import ValhallaClient

# A handle, never a coordinate. The pattern is defence in depth; the real
# guarantee is that the table only resolves strings it minted.
HANDLE_PATTERN = r"^(place|poi|current|centre|loop):[A-Za-z0-9_-]+$"

# The constraint has to live on the *item* type, not on Field(). A
# Field(pattern=...) attached to a list[str] is not applied to its items:
# it is silently missing from the generated schema, and pydantic raises
# TypeError rather than a validation error when it tries to apply it to
# the list. That would have turned a model-invented coordinate into a 500
# instead of a correction the model could act on.
HandleRef = Annotated[str, StringConstraints(pattern=HANDLE_PATTERN)]

# Names are shown to the model, so they are capped: an OSM name is public
# text and there is no reason to let a long one dominate the context.
MAX_NAME_CHARS = 120
PoiCategory = Literal[
    "cafe",
    "water",
    "pub",
    "food",
    "toilets",
    "bike_shop",
    "bike_repair",
    "supermarket",
    "viewpoint",
    "picnic",
    "campsite",
]
POI_RADIUS_M = 500.0


def _clip(name: str | None) -> str:
    if not name:
        return "unnamed"
    return name[:MAX_NAME_CHARS]


class SearchPlaceArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=2, max_length=120)
    limit: int = Field(default=5, ge=1, le=8)


class FindPoisArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    categories: list[PoiCategory] = Field(min_length=1, max_length=6)
    max_results: int = Field(default=10, ge=1, le=20)


class PlanRouteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    waypoint_refs: list[HandleRef] = Field(min_length=2, max_length=12)
    preset: Preset = "road"


class GenerateLoopArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin_ref: HandleRef
    target_km: float = Field(gt=0, le=200)
    preset: Preset = "road"


class RouteStatsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModifyRouteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["insert", "remove", "reverse"]
    # Required for insert; the reference to add.
    waypoint_ref: HandleRef | None = None
    # Required for insert and remove: 0-based position in the current list.
    position: int | None = Field(default=None, ge=0, le=50)


@dataclass
class ToolContext:
    """Everything the handlers need, assembled once per request."""

    db: AsyncSession
    valhalla: ValhallaClient
    handles: HandleTable
    waypoints: list[Waypoint]
    preset: Preset = "road"
    costing_options: BicycleCostingOptions | None = None
    centre: Waypoint | None = None
    search_enabled: bool = True
    # The most recent route the assistant produced, and the waypoints behind
    # it. This becomes the proposal the rider accepts or discards.
    route: RouteResponse | None = None
    proposed_waypoints: list[Waypoint] = field(default_factory=list)


def tool_schemas() -> list[dict[str, Any]]:
    """OpenAI-format function definitions, generated from the models above so
    the schema the model sees can never drift from the one that validates."""
    return [
        _schema(
            "search_place",
            "Find a place by name. Returns reference strings to use with the "
            "other tools. You must call this before routing anywhere you do "
            "not already have a reference for.",
            SearchPlaceArgs,
        ),
        _schema(
            "find_pois",
            "Find useful stops near the currently planned route, such as "
            "water, cafes or bike shops. Requires a planned route.",
            FindPoisArgs,
        ),
        _schema(
            "plan_route",
            "Plan a cycling route through the given references, in order. "
            "Returns the measured distance, climb and surface split.",
            PlanRouteArgs,
        ),
        _schema(
            "generate_loop",
            "Generate circular route options of roughly a target distance "
            "starting and finishing at one reference.",
            GenerateLoopArgs,
        ),
        _schema(
            "route_stats",
            "Report the measured figures for the currently planned route.",
            RouteStatsArgs,
        ),
        _schema(
            "modify_route",
            "Adjust the planned route: insert a reference at a position, "
            "remove the waypoint at a position, or reverse the direction.",
            ModifyRouteArgs,
        ),
    ]


def _schema(name: str, description: str, model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.pop("title", None)
    # Gateways vary in how they handle $defs in function parameters; the
    # models here are flat, so inlining keeps the payload portable.
    schema.setdefault("properties", {})
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": schema},
    }


async def run_tool(name: str, arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Dispatch one tool call.

    Every foreseeable failure - a bad argument, an unknown handle, Valhalla
    refusing - comes back as a result the model can read and correct, not an
    exception. Only genuinely unexpected errors escape.
    """
    handler = _HANDLERS.get(name)
    if handler is None:
        return {
            "error": "unknown_tool",
            "message": f"There is no tool called {name}. Available: {', '.join(_HANDLERS)}.",
        }
    try:
        return await handler(arguments, ctx)
    except ValidationError as exc:
        # The commonest failure by far, and entirely recoverable: the model
        # gets the specific complaint back and can call again. Only the
        # field and the reason, not pydantic's full error payload.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'arguments'}: {err['msg']}"
            for err in exc.errors()[:4]
        )
        return {"error": "invalid_arguments", "message": problems}
    except UnknownHandleError as exc:
        return {"error": "unknown_reference", "message": str(exc)}
    except HTTPException as exc:
        # 503 from an unreachable Valhalla, 422 for an unroutable request.
        return {"error": "tool_failed", "status": exc.status_code, "message": str(exc.detail)}


async def _search_place(raw: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    args = SearchPlaceArgs.model_validate(raw)
    if not ctx.search_enabled:
        return {
            "error": "unavailable",
            "message": "The place index is not built on this install, so places "
            "cannot be looked up by name.",
        }
    near = (ctx.centre.lat, ctx.centre.lon) if ctx.centre else None
    found = await search_places(ctx.db, args.query, near=near, limit=args.limit)
    results = []
    for place in found:
        handle = ctx.handles.mint("place", _clip(place.name), place.lat, place.lon)
        results.append(
            {
                "ref": handle.ref,
                "name": handle.name,
                "type": place.place_type,
                "distance_km": (
                    round(place.distance_m / 1000.0, 1) if place.distance_m is not None else None
                ),
            }
        )
    return {"results": results, "count": len(results)}


async def _find_pois(raw: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    args = FindPoisArgs.model_validate(raw)
    if not ctx.search_enabled:
        return {
            "error": "unavailable",
            "message": "The place index is not built on this install, so points of "
            "interest are not available.",
        }
    if ctx.route is None:
        return {
            "error": "no_route",
            "message": "Plan a route first - points of interest are searched along it.",
        }
    line = _route_line(ctx.route)
    found = await pois_along_route(ctx.db, line, POI_RADIUS_M, list(args.categories))
    results = []
    for poi in found.pois[: args.max_results]:
        handle = ctx.handles.mint("poi", _clip(poi.name), poi.lat, poi.lon)
        results.append(
            {
                "ref": handle.ref,
                "name": handle.name,
                "category": poi.category,
                "distance_along_km": round(poi.dist_along_m / 1000.0, 1),
                "distance_from_route_m": round(poi.dist_from_route_m),
            }
        )
    # Deliberately no `tags`: raw OpenStreetMap key/values are public-authored
    # free text and the largest untrusted surface here.
    return {"results": results, "count": len(results)}


async def _plan_route(raw: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    args = PlanRouteArgs.model_validate(raw)
    waypoints: list[Waypoint] = []
    for ref in args.waypoint_refs:
        waypoints.extend(ctx.handles.waypoints(ref))
    if len(waypoints) < 2:
        return {
            "error": "invalid_arguments",
            "message": "A route needs at least two distinct points.",
        }
    route = await ctx.valhalla.route(
        RouteRequest(
            waypoints=waypoints,
            preset=args.preset,
            costing_options=ctx.costing_options,
        )
    )
    ctx.route = route
    ctx.proposed_waypoints = waypoints
    ctx.preset = args.preset
    return _stats(route)


async def _generate_loop(raw: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    args = GenerateLoopArgs.model_validate(raw)
    origin = ctx.handles.point(args.origin_ref)
    candidates = await generate_loop_candidates(
        origin=origin,
        target_km=args.target_km,
        preset=args.preset,
        costing_options=ctx.costing_options,
        valhalla=ctx.valhalla,
    )
    if not candidates:
        return {
            "error": "no_loops",
            "message": "No loop of about that distance could be found from there. "
            "Try a different distance or a different starting point.",
        }
    results = []
    for candidate in candidates:
        handle = ctx.handles.mint(
            "loop",
            f"{round(candidate.snapshot.distance_m / 1000.0, 1)} km loop",
            origin.lat,
            origin.lon,
            waypoints=candidate.waypoints,
        )
        results.append({"ref": handle.ref, **_stats(candidate.snapshot)})
    # The first candidate is the best-scoring one, so adopting it without a
    # further call is a reasonable default rather than an arbitrary pick.
    best = candidates[0]
    ctx.route = best.snapshot
    ctx.proposed_waypoints = list(best.waypoints)
    ctx.preset = args.preset
    return {
        "candidates": results,
        "note": "The first candidate is currently selected. Call plan_route with "
        "another candidate's ref to select it instead.",
    }


async def _route_stats(raw: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    RouteStatsArgs.model_validate(raw)
    if ctx.route is None:
        return {"error": "no_route", "message": "No route is planned yet."}
    return _stats(ctx.route)


async def _modify_route(raw: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    args = ModifyRouteArgs.model_validate(raw)
    current = list(ctx.proposed_waypoints or ctx.waypoints)
    if len(current) < 2:
        return {"error": "no_route", "message": "There is no route to modify yet."}

    if args.action == "reverse":
        # Re-routed rather than geometry-flipped, so one-ways and turn cues
        # stay correct - the same rule the library's Reverse follows.
        current.reverse()
    elif args.action == "remove":
        if args.position is None or args.position >= len(current):
            return {
                "error": "invalid_arguments",
                "message": f"position must be between 0 and {len(current) - 1}.",
            }
        if len(current) <= 2:
            return {
                "error": "invalid_arguments",
                "message": "A route needs at least two points, so this one cannot lose any.",
            }
        current.pop(args.position)
    else:
        if args.waypoint_ref is None:
            return {
                "error": "invalid_arguments",
                "message": "insert needs waypoint_ref.",
            }
        position = len(current) - 1 if args.position is None else min(args.position, len(current))
        current.insert(position, ctx.handles.point(args.waypoint_ref))

    route = await ctx.valhalla.route(
        RouteRequest(waypoints=current, preset=ctx.preset, costing_options=ctx.costing_options)
    )
    ctx.route = route
    ctx.proposed_waypoints = current
    return _stats(route)


def _route_line(route: RouteResponse) -> list[tuple[float, float]]:
    """The route shape as (lon, lat) pairs.

    decode_polyline6 yields (lat, lon) and the PostGIS helpers want
    (lon, lat) - getting this backwards would search for points of interest
    somewhere else entirely and still return a plausible-looking list.
    """
    shape = concat_shapes([decode_polyline6(leg.geometry) for leg in route.legs])
    return [(lon, lat) for lat, lon in shape]


def _stats(route: RouteResponse) -> dict[str, Any]:
    """Only measured figures, and only ones the rider would recognise."""
    result: dict[str, Any] = {
        "distance_km": round(route.distance_m / 1000.0, 1),
        "ascent_m": round(route.ascent_m),
        "descent_m": round(route.descent_m),
        "waypoint_count": len(route.legs) + 1,
    }
    if route.surface is not None and route.surface.total_m > 0:
        paved = sum(
            metres for name, metres in route.surface.surface_m.items() if name.startswith("paved")
        )
        result["paved_percent"] = round(100.0 * paved / route.surface.total_m)
    if route.climbs:
        result["climbs"] = [
            {"category": climb.category, "length_km": round(climb.length_m / 1000.0, 1)}
            for climb in route.climbs[:5]
        ]
    return result


_HANDLERS = {
    "search_place": _search_place,
    "find_pois": _find_pois,
    "plan_route": _plan_route,
    "generate_loop": _generate_loop,
    "route_stats": _route_stats,
    "modify_route": _modify_route,
}
