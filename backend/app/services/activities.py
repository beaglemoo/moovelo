"""Turning a parsed file into a stored ride.

Deliberately thin, and deliberately free of Valhalla. An activity is stored
as it was recorded: no map matching, no elevation backfill, no re-routing.
The rider's own barometer beats a DEM lookup, and a ride whose GPS wandered
is still that ride. Matching enters the picture only where coverage needs it.
"""

import re
import uuid

from geoalchemy2 import WKTElement

from app.models import Activity
from app.services.geo import cumulative_distances
from app.services.importer import ImportedTrack, elevation_profile
from app.services.valhalla import ascent_descent

NAME_MAX = 200

# "12345678901.fit.gz", "12345678901.gpx" - Strava names every exported file
# after the activity id it belongs to.
_STRAVA_ID = re.compile(r"^(\d{6,})\.")


def strava_activity_id(filename: str) -> str | None:
    """The Strava activity id a bulk-export filename is named for, if any."""
    match = _STRAVA_ID.match(filename.rsplit("/", 1)[-1])
    return match.group(1) if match else None


def track_geometry(track: ImportedTrack) -> WKTElement:
    """The recorded trace as a PostGIS linestring."""
    coords = ", ".join(f"{lon} {lat}" for lat, lon in track.shape)
    return WKTElement(f"LINESTRING({coords})", srid=4326)


def activity_from_track(
    track: ImportedTrack,
    *,
    user_id: uuid.UUID,
    fallback_name: str,
    source: str = "file",
    source_ref: str | None = None,
) -> Activity:
    """Build an unsaved Activity from a parsed track."""
    elevation = elevation_profile(track)
    ascent, descent = ascent_descent(elevation)
    distances = cumulative_distances(track.shape)

    name = (track.name or fallback_name).strip()[:NAME_MAX] or fallback_name[:NAME_MAX]
    return Activity(
        # Assigned eagerly rather than left to the column default: both
        # import paths queue map matching by id straight after this object
        # is created, and the default only populates it at flush.
        id=uuid.uuid4(),
        user_id=user_id,
        name=name,
        started_at=track.started_at,
        elapsed_time_s=track.elapsed_time_s,
        moving_time_s=track.moving_time_s,
        distance_m=distances[-1] if distances else 0.0,
        ascent_m=ascent,
        descent_m=descent,
        elevation=[point.model_dump() for point in elevation],
        geom=track_geometry(track),
        source=source,
        source_ref=source_ref,
        import_status="done",
    )


def name_from_filename(filename: str) -> str:
    """A readable fallback when the file names neither itself nor its ride."""
    stem = filename.rsplit("/", 1)[-1]
    # Strava exports arrive gzipped, so the extension worth dropping is the
    # second one: "4821.fit.gz" should not become "4821.fit".
    if stem.lower().endswith(".gz"):
        stem = stem[: -len(".gz")]
    stem = stem.rsplit(".", 1)[0]
    cleaned = " ".join(stem.replace("_", " ").replace("-", " ").split())
    return cleaned[:NAME_MAX] or "Imported ride"
