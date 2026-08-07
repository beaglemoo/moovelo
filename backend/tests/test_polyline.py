from app.services.polyline import decode_polyline6, encode_polyline6


def test_roundtrip() -> None:
    coords = [(53.7996, -1.5491), (53.8008, -1.5523), (53.7950, -1.5600)]
    decoded = decode_polyline6(encode_polyline6(coords))
    assert len(decoded) == len(coords)
    for (lat1, lon1), (lat2, lon2) in zip(coords, decoded, strict=True):
        assert abs(lat1 - lat2) < 1e-6
        assert abs(lon1 - lon2) < 1e-6


def test_empty() -> None:
    assert decode_polyline6("") == []
