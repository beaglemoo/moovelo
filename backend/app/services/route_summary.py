"""Natural-language route summary for public share pages.

Generated exactly once, when the route's *owner* creates or rotates a share
link (see api/routes.py's share_route) - never on the public, unauthenticated
read path. That split matters: /api/shared/{token} takes no session and
anyone can hit it, so if generation lived there an anonymous request could
spend the operator's money on every page view. Here it is an authenticated,
owner-triggered write, and afterwards the summary is served as the plain
stored text it already is.

Unlike weather.py this makes no raw HTTP call of its own: LLMClient already
owns the retry policy (429/5xx, MAX_ATTEMPTS=3) and its own LLMError, so
there is nothing to add here except catching that error and degrading to no
summary - a share must always succeed even when this fails entirely.
"""

import hashlib
import logging

from app.models import Route
from app.services.llm import LLMClient, LLMError
from app.services.llm_config import LLMConfig

logger = logging.getLogger(__name__)

# A short paragraph, not a chat turn - generous enough for a slow provider
# without holding the share-creation request open indefinitely.
SUMMARY_TIMEOUT_S = 30.0

SYSTEM_PROMPT = (
    "You write a short, welcoming summary of a cycling route for a public "
    "share page that strangers will read before riding it. Two to three "
    "plain-prose sentences, no markdown, no headings, no bullet points. "
    "Mention the distance and elevation gain, and anything notable about "
    "surface or climbs if given. Use only the facts provided - never invent "
    "place names, road names or terrain details that were not given to you."
)


def route_geometry_signature(route: Route) -> str:
    """Hash of the route's own shape - the concatenated leg polylines, the
    same geometry a viewer sees on the map. Stats, name, tags and notes can
    all change without this changing, and none of them are what the summary
    describes; a re-route always changes this, which is exactly the case
    that must invalidate a previously generated summary.
    """
    raw = "|".join(leg.get("geometry", "") for leg in route.legs)
    return hashlib.sha256(raw.encode()).hexdigest()


def _surface_highlights(surface: dict[str, object] | None) -> str | None:
    total = surface.get("total_m") if surface else None
    surface_m = surface.get("surface_m") if surface else None
    if not isinstance(total, int | float) or total <= 0 or not isinstance(surface_m, dict):
        return None
    # Top two categories by distance is enough to describe the mix without
    # listing Valhalla's whole surface enum at the model.
    ranked = sorted(
        ((name, m) for name, m in surface_m.items() if isinstance(m, int | float) and m > 0),
        key=lambda pair: pair[1],
        reverse=True,
    )[:2]
    if not ranked:
        return None
    return ", ".join(f"{m / total * 100:.0f}% {name.replace('_', ' ')}" for name, m in ranked)


def _stats_prompt(route: Route) -> str:
    lines = [
        f"Distance: {route.distance_m / 1000:.1f} km",
        f"Elevation gain: {route.ascent_m:.0f} m",
        f"Elevation loss: {route.descent_m:.0f} m",
        f"Routing preference: {route.preset}",
    ]
    surface = _surface_highlights(route.surface)
    if surface:
        lines.append(f"Surface: {surface}")
    if route.climbs:
        categories = ", ".join(sorted({c.get("category", "?") for c in route.climbs}))
        lines.append(f"Climbs: {len(route.climbs)} (categories: {categories})")
    return "\n".join(lines)


async def generate_summary(route: Route, config: LLMConfig) -> str | None:
    """One completion, no tools. Returns None - never raises - when the
    assistant is not configured or the call fails outright: sharing a route
    must always succeed even when a summary cannot be produced."""
    if not config.enabled:
        return None
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _stats_prompt(route)},
    ]
    try:
        async with LLMClient(config) as llm:
            result = await llm.complete(messages, timeout=SUMMARY_TIMEOUT_S)
    except LLMError:
        logger.warning("Route summary generation failed", exc_info=True)
        return None
    content = result.content.strip()
    return content or None
