"""The config-surface guard's own tests.

deploy/check-config-surface.py exists to make one bug family fail CI instead
of users: a variable declared in one place and silently ignored in another.
It shipped with a green CI job and nothing checking the guard itself, which
is the worst state for a guard to be in - a blind spot in it reads as "all
clear" rather than as a failure.

So these tests do two things: prove the guard actually fires on each bug it
claims to catch (by feeding it a deliberately broken synthetic repo), and
pin the field scanner against the blind spot review found - a field declared
inside a nested block was invisible, so it was never checked at all.

Lives under backend/tests rather than beside the script because this is where
CI already runs pytest. The script itself stays dependency-free.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD_PATH = REPO_ROOT / "deploy" / "check-config-surface.py"


def _load_guard() -> Any:
    """A fresh module each time, so a test that repoints REPO_ROOT cannot
    leak that into the next one."""
    spec = importlib.util.spec_from_file_location("config_surface_guard", GUARD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def guard() -> Any:
    return _load_guard()


def _write_settings(tmp_path: Path, body: str) -> Path:
    config = tmp_path / "config.py"
    config.write_text(
        "from pydantic_settings import BaseSettings\n\n\nclass Settings(BaseSettings):\n" + body
    )
    return config


def test_a_field_in_a_nested_block_is_still_a_field(guard: Any, tmp_path: Path) -> None:
    """The blind spot. A field inside an `if` is an ordinary field at
    runtime, so the guard must check it like any other - it used to not even
    see it, and an unseen field is one the guard silently never checks."""
    config = _write_settings(
        tmp_path,
        '    plain: str = "a"\n'
        "    if True:\n"
        '        gated: str = "b"\n'
        "    try:\n"
        '        attempted: str = "c"\n'
        "    except Exception:\n"
        '        recovered: str = "d"\n',
    )
    assert guard.settings_fields(config) == {"plain", "gated", "attempted", "recovered"}


def test_a_name_bound_inside_a_method_is_not_a_field(guard: Any, tmp_path: Path) -> None:
    """The other half of the same change: descending into blocks must not
    become descending into everything. A local in a method reads no
    environment variable, and demanding an .env.example entry for one would
    make the guard cry wolf."""
    config = _write_settings(
        tmp_path,
        '    real: str = "a"\n'
        "    def helper(self) -> None:\n"
        "        local: int = 1\n"
        "    class Nested:\n"
        "        inner: int = 2\n",
    )
    assert guard.settings_fields(config) == {"real"}


def test_a_property_is_not_a_field(guard: Any, tmp_path: Path) -> None:
    """Computed values read other fields, not the environment."""
    config = _write_settings(
        tmp_path,
        '    real: str = "a"\n'
        "    @property\n"
        "    def derived(self) -> str:\n"
        "        return self.real\n",
    )
    assert guard.settings_fields(config) == {"real"}


def _fake_repo(tmp_path: Path, env_example: str, compose: str) -> Path:
    (tmp_path / ".env.example").write_text(env_example)
    (tmp_path / "docker-compose.yml").write_text(compose)
    return tmp_path


COMPOSE_WIRED = """services:
  api:
    image: example
    environment:
      WIDGET_URL: ${WIDGET_URL:-http://localhost}
"""


def test_a_field_missing_from_env_example_is_reported(guard: Any, tmp_path: Path) -> None:
    """Rule 1, mutation-tested: the field is wired through compose but never
    documented."""
    config = _write_settings(tmp_path, '    widget_url: str = "http://localhost"\n')
    guard.REPO_ROOT = _fake_repo(tmp_path, "# nothing documented here\n", COMPOSE_WIRED)

    problems = guard.check("test", config, ["api"], set(), set())

    assert any("WIDGET_URL= entry in .env.example" in p for p in problems), problems


def test_a_field_missing_from_a_compose_block_is_reported(guard: Any, tmp_path: Path) -> None:
    """Rule 2, mutation-tested. This is the WEATHER_API_URL bug exactly:
    documented, declared, and never passed to the container."""
    config = _write_settings(tmp_path, '    widget_url: str = "http://localhost"\n')
    guard.REPO_ROOT = _fake_repo(
        tmp_path,
        "WIDGET_URL=http://localhost\n",
        "services:\n  api:\n    image: example\n    environment:\n      OTHER: ${OTHER:-x}\n",
    )

    problems = guard.check("test", config, ["api"], set(), set())

    assert any("is not in the `api` environment block" in p for p in problems), problems


def test_a_hardcoded_compose_value_is_reported(guard: Any, tmp_path: Path) -> None:
    """Rule 3, mutation-tested. The VALHALLA_URL shape: the key is present
    and documented, but its value is a literal, so setting it does nothing.
    This one passes both of the other two rules, which is why it needs its
    own."""
    config = _write_settings(tmp_path, '    widget_url: str = "http://localhost"\n')
    guard.REPO_ROOT = _fake_repo(
        tmp_path,
        "WIDGET_URL=http://localhost\n",
        "services:\n  api:\n    image: example\n    environment:\n"
        "      WIDGET_URL: http://hardcoded:8080\n",
    )

    problems = guard.check("test", config, ["api"], set(), set())

    assert any("hardcoded literal" in p for p in problems), problems


def test_a_correctly_wired_field_is_reported_clean(guard: Any, tmp_path: Path) -> None:
    """The negative control. Without this, every assertion above would still
    pass if check() simply complained about everything."""
    config = _write_settings(tmp_path, '    widget_url: str = "http://localhost"\n')
    guard.REPO_ROOT = _fake_repo(tmp_path, "WIDGET_URL=http://localhost\n", COMPOSE_WIRED)

    assert guard.check("test", config, ["api"], set(), set()) == []


def test_the_real_repo_passes(guard: Any) -> None:
    """The guard is only useful if it is green on a correct tree - a guard
    that fails constantly gets its CI job deleted."""
    assert guard.main() == 0
