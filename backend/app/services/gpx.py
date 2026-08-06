"""GPX 1.1 track export."""

from xml.etree import ElementTree as ET

from app.schemas import ElevationPoint
from app.services.geo import Point, cumulative_distances

GPX_NS = "http://www.topografix.com/GPX/1/1"


def interpolate_elevations(shape: list[Point], profile: list[ElevationPoint]) -> list[float | None]:
    """Elevation for each shape vertex, interpolated from the stored profile."""
    if not profile:
        return [None] * len(shape)
    dists = cumulative_distances(shape)
    result: list[float | None] = []
    j = 0
    for d in dists:
        while j + 1 < len(profile) and profile[j + 1].dist_m < d:
            j += 1
        if j + 1 >= len(profile):
            result.append(profile[-1].elev_m)
            continue
        a, b = profile[j], profile[j + 1]
        span = b.dist_m - a.dist_m
        t = (d - a.dist_m) / span if span > 0 else 0.0
        result.append(a.elev_m + (b.elev_m - a.elev_m) * t)
    return result


def build_gpx(name: str, shape: list[Point], profile: list[ElevationPoint]) -> bytes:
    ET.register_namespace("", GPX_NS)
    gpx = ET.Element(f"{{{GPX_NS}}}gpx", attrib={"version": "1.1", "creator": "komoot-lite"})
    metadata = ET.SubElement(gpx, f"{{{GPX_NS}}}metadata")
    ET.SubElement(metadata, f"{{{GPX_NS}}}name").text = name
    trk = ET.SubElement(gpx, f"{{{GPX_NS}}}trk")
    ET.SubElement(trk, f"{{{GPX_NS}}}name").text = name
    trkseg = ET.SubElement(trk, f"{{{GPX_NS}}}trkseg")
    elevations = interpolate_elevations(shape, profile)
    for (lat, lon), ele in zip(shape, elevations, strict=True):
        trkpt = ET.SubElement(
            trkseg, f"{{{GPX_NS}}}trkpt", attrib={"lat": f"{lat:.6f}", "lon": f"{lon:.6f}"}
        )
        if ele is not None:
            ET.SubElement(trkpt, f"{{{GPX_NS}}}ele").text = f"{ele:.1f}"
    ET.indent(gpx)
    data: bytes = ET.tostring(gpx, encoding="utf-8", xml_declaration=True)
    return data
