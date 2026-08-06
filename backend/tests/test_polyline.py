from app.services.polyline import decode_polyline6


def encode_polyline6(coords: list[tuple[float, float]]) -> str:
    out: list[str] = []
    prev_lat = prev_lon = 0
    for lat, lon in coords:
        for value, prev in ((round(lat * 1e6), prev_lat), (round(lon * 1e6), prev_lon)):
            delta = value - prev
            delta = ~(delta << 1) if delta < 0 else delta << 1
            while delta >= 0x20:
                out.append(chr((0x20 | (delta & 0x1F)) + 63))
                delta >>= 5
            out.append(chr(delta + 63))
        prev_lat, prev_lon = round(lat * 1e6), round(lon * 1e6)
    return "".join(out)


def test_roundtrip() -> None:
    coords = [(53.7996, -1.5491), (53.8008, -1.5523), (53.7950, -1.5600)]
    decoded = decode_polyline6(encode_polyline6(coords))
    assert len(decoded) == len(coords)
    for (lat1, lon1), (lat2, lon2) in zip(coords, decoded, strict=True):
        assert abs(lat1 - lat2) < 1e-6
        assert abs(lon1 - lon2) < 1e-6


def test_empty() -> None:
    assert decode_polyline6("") == []
