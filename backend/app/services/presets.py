"""Bicycle costing-option bundles for the three UI presets.

Each preset is a plain dict of Valhalla `costing_options.bicycle` values, sent
per request. Keeping them as flat option dicts means any single option can be
promoted to a user-facing slider later without an API change.

Option semantics (Valhalla docs):
- bicycle_type: sets the base speed model and which surfaces are rideable at
  all (Road < Hybrid < Cross < Mountain in surface tolerance).
- cycling_speed: km/h on flat smooth ground; drives ETAs.
- use_roads [0..1]: 0 = strongly prefer cycleways/paths, 1 = happy on big roads.
- use_hills [0..1]: 0 = detour around climbs, 1 = don't care about grade.
- avoid_bad_surfaces [0..1]: 1 = hard-avoid anything worse than the bike's
  preferred surface, 0 = only penalise truly unrideable surfaces.
"""

from app.schemas import Preset

BicycleOptions = dict[str, str | float | int]

PRESETS: dict[Preset, BicycleOptions] = {
    # Road: fast tarmac riding. Comfortable sharing carriageways (0.6) but a
    # skinny-tyre bike must stay off gravel, so bad surfaces are near-hard
    # avoided (0.9). Neutral on hills - roadies climb.
    "road": {
        "bicycle_type": "Road",
        "cycling_speed": 25,
        "use_roads": 0.6,
        "use_hills": 0.5,
        "avoid_bad_surfaces": 0.9,
    },
    # Gravel: seek out unpaved. Cross bike tolerates rough surfaces, zero
    # surface penalty so towpaths/bridleways/forest tracks are welcome, and a
    # low use_roads biases away from tarmac where an off-road option exists.
    "gravel": {
        "bicycle_type": "Cross",
        "cycling_speed": 20,
        "use_roads": 0.3,
        "use_hills": 0.5,
        "avoid_bad_surfaces": 0.0,
    },
    # Quiet: relaxed riding on cycleways and back streets. use_roads 0.1
    # strongly prefers dedicated infrastructure, use_hills 0.3 softens climbs,
    # moderate surface avoidance keeps it on decent paths without banning
    # the odd compacted-gravel greenway.
    "quiet": {
        "bicycle_type": "Hybrid",
        "cycling_speed": 18,
        "use_roads": 0.1,
        "use_hills": 0.3,
        "avoid_bad_surfaces": 0.4,
    },
}
