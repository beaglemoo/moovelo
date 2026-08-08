import pytest
from pydantic import ValidationError

from app.schemas import BicycleCostingOptions
from app.services.presets import PRESETS, resolve_costing

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


def test_resolve_costing_uses_the_named_preset_when_no_custom_given() -> None:
    for name in PRESETS:
        assert resolve_costing(name, None) == PRESETS[name]


def test_resolve_costing_prefers_custom_over_the_named_preset() -> None:
    custom = BicycleCostingOptions(
        bicycle_type="Mountain",
        cycling_speed=15,
        use_roads=0.2,
        use_hills=0.9,
        avoid_bad_surfaces=0.0,
    )
    assert resolve_costing("road", custom) == custom.model_dump()


def test_bicycle_costing_options_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        BicycleCostingOptions.model_validate(
            {
                "bicycle_type": "Road",
                "cycling_speed": 20,
                "use_roads": 0.5,
                "use_hills": 0.5,
                "avoid_bad_surfaces": 0.5,
                "extra_field": 1,
            }
        )


def test_bicycle_costing_options_rejects_out_of_range_values() -> None:
    with pytest.raises(ValidationError):
        BicycleCostingOptions(
            bicycle_type="Road",
            cycling_speed=200,
            use_roads=0.5,
            use_hills=0.5,
            avoid_bad_surfaces=0.5,
        )
