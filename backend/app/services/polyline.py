"""Decode Valhalla encoded polylines (precision 6)."""


def decode_polyline6(encoded: str) -> list[tuple[float, float]]:
    """Decode to a list of (lat, lon) tuples."""
    coords: list[tuple[float, float]] = []
    lat = lon = 0
    i = 0
    length = len(encoded)
    while i < length:
        for is_lon in (False, True):
            shift = 0
            result = 0
            while True:
                b = ord(encoded[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if is_lon:
                lon += delta
            else:
                lat += delta
        coords.append((lat / 1e6, lon / 1e6))
    return coords
