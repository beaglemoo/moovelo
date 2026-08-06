from app.services.presets import PRESETS

VALID_OPTIONS = {"bicycle_type", "cycling_speed", "use_roads", "use_hills", "avoid_bad_surfaces"}
VALID_BICYCLE_TYPES = {"Road", "Hybrid", "Cross", "Mountain"}


def test_all_presets_defined() -> None:
    assert set(PRESETS) == {"road", "gravel", "quiet"}


def test_option_names_and_ranges() -> None:
    for name, options in PRESETS.items():
        assert set(options) == VALID_OPTIONS, name
        assert options["bicycle_type"] in VALID_BICYCLE_TYPES
        for key in ("use_roads", "use_hills", "avoid_bad_surfaces"):
            value = options[key]
            assert isinstance(value, int | float) and 0 <= value <= 1, f"{name}.{key}"
        speed = options["cycling_speed"]
        assert isinstance(speed, int | float) and 10 <= speed <= 35


def test_presets_are_distinct() -> None:
    bundles = [tuple(sorted(options.items())) for options in PRESETS.values()]
    assert len(set(bundles)) == len(bundles)
