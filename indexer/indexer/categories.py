"""What gets indexed, and how it ranks.

This module is the single source of truth for both halves of the job: the
`osmium tags-filter` expressions that shrink the extract, and the mapping
from OSM tags to our own categories. Deriving the filter from the mapping
means the two cannot drift - a category added here is automatically kept
by the filter, rather than being silently absent from the filtered file.
"""

import math

# Settlement and feature types worth searching for, weighted so that a
# search for "tring" puts the town above a hamlet or a station of the same
# name. Population, where OSM carries it, only breaks ties within a type.
PLACE_IMPORTANCE: dict[str, float] = {
    "city": 0.85,
    "borough": 0.70,
    "town": 0.65,
    "suburb": 0.45,
    "village": 0.40,
    "neighbourhood": 0.30,
    "hamlet": 0.25,
    "locality": 0.15,
    "isolated_dwelling": 0.10,
    # Not place=* values, but people search for them by name all the same.
    "peak": 0.30,
    "station": 0.25,
}

POPULATION_BONUS = 0.15

# Tags that make a node or way a searchable place, as {key: {value: type}}.
PLACE_TAGS: dict[str, dict[str, str]] = {
    "place": {
        value: value
        for value in (
            "city",
            "borough",
            "town",
            "suburb",
            "village",
            "neighbourhood",
            "hamlet",
            "locality",
            "isolated_dwelling",
        )
    },
    "natural": {"peak": "peak"},
    "railway": {"station": "station", "halt": "station"},
}

# Tags that make something worth stopping at on a ride, as
# {key: {value: category}}. Curated for cycling rather than exhaustive:
# every extra value is rows in the database and noise in the results.
POI_TAGS: dict[str, dict[str, str]] = {
    "amenity": {
        "cafe": "cafe",
        "pub": "pub",
        "bar": "pub",
        "restaurant": "food",
        "fast_food": "food",
        "drinking_water": "water",
        "toilets": "toilets",
        "bicycle_repair_station": "bike_repair",
        "bicycle_parking": "bike_parking",
        "pharmacy": "pharmacy",
    },
    "shop": {
        "bicycle": "bike_shop",
        "supermarket": "supermarket",
        "convenience": "shop",
        "bakery": "bakery",
    },
    "tourism": {
        "viewpoint": "viewpoint",
        "picnic_site": "picnic",
        "camp_site": "campsite",
        "hotel": "accommodation",
        "guest_house": "accommodation",
    },
}

# Signed cycle networks, most significant first. icn/ncn are drawn at low
# zoom; rcn/lcn only once the map is close enough to be useful.
CYCLE_NETWORKS = ("icn", "ncn", "rcn", "lcn")

# OSM tags worth keeping verbatim on a POI. Opening hours in particular
# answer "will the cafe be open when I get there", which is the whole
# point of knowing the cafe is there.
KEPT_POI_TAGS = (
    "opening_hours",
    "phone",
    "website",
    "operator",
    "cuisine",
    "drinking_water",
    "fee",
    "wheelchair",
)


def filter_expressions() -> list[str]:
    """The `osmium tags-filter` arguments, derived from the tables above.

    Places are looked for on nodes only: a town mapped as an administrative
    boundary relation has a node too, and taking both would double every
    settlement in the results.
    """
    expressions = [f"n/{key}={','.join(sorted(values))}" for key, values in PLACE_TAGS.items()]
    expressions += [f"nw/{key}={','.join(sorted(values))}" for key, values in POI_TAGS.items()]
    expressions.append("r/route=bicycle")
    return expressions


def place_type(tags: dict[str, str]) -> str | None:
    for key, values in PLACE_TAGS.items():
        mapped = values.get(tags.get(key, ""))
        if mapped is not None:
            return mapped
    return None


def poi_category(tags: dict[str, str]) -> str | None:
    for key, values in POI_TAGS.items():
        mapped = values.get(tags.get(key, ""))
        if mapped is not None:
            return mapped
    return None


def parse_population(raw: str | None) -> int | None:
    """OSM population values are free text: "12,000", "~800", "1 234"."""
    if not raw:
        return None
    digits = "".join(character for character in raw if character.isdigit())
    if not digits:
        return None
    try:
        population = int(digits)
    except ValueError:  # pragma: no cover - digits-only cannot fail
        return None
    # A population of a hundred million is a typo, not a settlement.
    return population if 0 < population < 100_000_000 else None


def importance(place: str, population: int | None) -> float:
    base = PLACE_IMPORTANCE.get(place, 0.1)
    if population is None or population <= 0:
        return base
    # log10 so a city of a million outranks a town of ten thousand without
    # swamping the type weighting entirely.
    return base + POPULATION_BONUS * min(1.0, math.log10(population) / 6.0)


def cycle_network(tags: dict[str, str]) -> str | None:
    if tags.get("type") != "route" or tags.get("route") != "bicycle":
        return None
    network = tags.get("network", "").lower()
    return network if network in CYCLE_NETWORKS else None
