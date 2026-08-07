"""Encode and decode Valhalla polylines (precision 6)."""


def encode_polyline6(coords: list[tuple[float, float]]) -> str:
    """Encode a list of (lat, lon) tuples.

    Needed when we have to build a route snapshot ourselves rather than
    receiving one from Valhalla - an imported track that could not be matched
    to the road network still has to be stored in the same wire format.
    """
    out: list[str] = []
    prev_lat = prev_lon = 0
    for lat, lon in coords:
        scaled_lat, scaled_lon = round(lat * 1e6), round(lon * 1e6)
        for delta in (scaled_lat - prev_lat, scaled_lon - prev_lon):
            value = ~(delta << 1) if delta < 0 else delta << 1
            while value >= 0x20:
                out.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            out.append(chr(value + 63))
        prev_lat, prev_lon = scaled_lat, scaled_lon
    return "".join(out)


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
