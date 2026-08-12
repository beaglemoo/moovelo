#!/usr/bin/env python3
"""Guard against the "documented but never wired" class of config bug.

This project has shipped three variables that were declared in one place
(a Settings class, .env.example) and silently ignored in another:
WEATHER_API_URL never reached the compose environment block (Phase 7),
OSM_DIR/WORK_DIR were documented as overridable but never passed to the
indexer (Phase 10), and the Caddy upload limits were documented in prose
that a later compose/config change didn't keep up with. Fixing the latest
instance doesn't retire the family - this script re-derives the three
surfaces (pydantic Settings fields, .env.example keys, compose environment
blocks) from source on every run and fails loudly the moment they diverge,
so the next missing wire-up is a red CI check instead of a support ticket.

Deliberately dependency-free (stdlib only: ast, re) so it needs no venv and
can run as its own CI job before either the backend or indexer job spins up
a database.

What it checks, for backend/app/config.py and indexer/indexer/config.py:
  1. Every Settings field (as SCREAMING_SNAKE_CASE - pydantic-settings'
     default env var name, since neither Settings subclass sets an
     env_prefix) appears as a key in .env.example.
  2. Every Settings field appears as an environment key in every compose
     service block that runs that config module - both backend-dev and
     backend for backend/app/config.py, indexer for
     indexer/indexer/config.py.
  3. Every key in .env.example is either a Settings field or explicitly
     allowlisted as an infra-only variable that no Settings class reads
     (e.g. POSTGRES_PASSWORD, VALHALLA_TILE_URL) - so a var can't be typo'd
     into .env.example and silently do nothing.

A field can be deliberately exempt from rule 1 or 2 - built compositionally
in compose rather than taken verbatim (DATABASE_URL), or a container-fixed
path with no legitimate deployment reason to override (STATIC_DIR,
OSMIUM_BINARY) - but only via the explicit allowlists below, never by
silent omission.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Fields intentionally not in .env.example / not passed through compose,
# with the reason. Add to these, don't just stop checking a field.
BACKEND_NO_ENV_EXAMPLE = {
    "database_url",  # built from POSTGRES_PASSWORD in compose, not a raw override
    "static_dir",  # fixed container path (/app/static), nothing to override
    "cors_origins",  # fixed by dev compose topology, not user config - see below
}
BACKEND_NO_COMPOSE = {
    "database_url",  # composed in-line, not read from an env var of the same name
    "static_dir",
    # Hardcoded to http://localhost:5173 in backend-dev only, because that's
    # frontend-dev's fixed Vite port - the value isn't a deployment choice,
    # it's dictated by the dev compose topology itself. Deliberately absent
    # from the prod `backend` block: prod serves the built frontend from the
    # same origin as the API, so cors_origins stays "" and main.py's
    # `if settings.cors_origins:` skips the CORS middleware entirely.
    "cors_origins",
}
# Compose services that run backend/app/config.py and must carry every field
# (minus BACKEND_NO_COMPOSE) in their `environment:` block.
BACKEND_COMPOSE_SERVICES = ["backend-dev", "backend"]

INDEXER_NO_ENV_EXAMPLE = {
    "database_url",  # built from POSTGRES_PASSWORD in compose
    "osmium_binary",  # fixed container binary path, nothing to override
}
INDEXER_NO_COMPOSE = {
    "database_url",
    "osmium_binary",
}
INDEXER_COMPOSE_SERVICES = ["indexer"]

# .env.example keys that are real, but are not Settings fields at all -
# Valhalla/Postgres/compose-level knobs with no corresponding config.py.
ENV_EXAMPLE_NON_SETTINGS_KEYS = {
    "VALHALLA_TILE_URL",
    "VALHALLA_BUILD_ELEVATION",
    "VALHALLA_SERVER_THREADS",
    "APP_PORT",
    "POSTGRES_PASSWORD",
}


def _fields_in_block(body: list[ast.stmt], fields: set[str]) -> None:
    """Annotated assignments anywhere in a class body except inside a def.

    A field declared inside an `if` or a `try` is a perfectly ordinary
    pydantic-settings field at runtime, so a scan that only looked at the
    class body's direct statements would not see it - and a field this guard
    cannot enumerate is a field it silently never checks, which is worse than
    no guard at all because the green tick claims otherwise.

    The first version of this fix listed the block types to descend into
    (if/try/with/for/while) and so reproduced that bug one construct over:
    `match` was not in the list, and a field inside a `case` was still
    invisible. Enumerating syntax is the wrong shape - the list is never
    finished, and each gap is silent. So the rule is inverted: descend into
    everything, and name only what must be skipped.

    What must be skipped is a def or a nested class. A name bound inside a
    method is a local, and demanding an .env.example entry for one would make
    the guard cry wolf. Descending through expression nodes is harmless - an
    annotated assignment is a statement and cannot appear inside one.
    """
    for item in body:
        _collect_fields(item, fields)


def _is_type_checking(test: ast.expr) -> bool:
    """`if TYPE_CHECKING:` / `if typing.TYPE_CHECKING:`, the one branch that
    is guaranteed never to run."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _collect_fields(node: ast.AST, fields: set[str]) -> None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        fields.add(node.target.id)
        return
    # A TYPE_CHECKING body never executes, so nothing declared in it is a
    # runtime attribute and pydantic never makes it a field. Counting one
    # would have the guard demand an .env.example entry and two compose
    # entries for a name that does not exist - the cry-wolf failure that
    # makes a guard get deleted. Its `else` branch DOES run, so that is still
    # descended into.
    #
    # This is the one dead branch worth special-casing, because it is the
    # only one that is idiomatic. A general "is this branch reachable"
    # question cannot be answered from the AST, and a guard that tried would
    # be guessing; anything more exotic than TYPE_CHECKING is out of scope
    # and will be over-reported rather than silently missed - the safe
    # direction for a guard to be wrong in.
    if isinstance(node, ast.If) and _is_type_checking(node.test):
        for statement in node.orelse:
            _collect_fields(statement, fields)
        return
    for child in ast.iter_child_nodes(node):
        _collect_fields(child, fields)


def settings_fields(config_path: Path) -> set[str]:
    """Annotated assignments in the Settings class, i.e. actual
    pydantic-settings fields - not @property computed values, which read
    other fields rather than the environment."""
    tree = ast.parse(config_path.read_text(), filename=str(config_path))
    fields: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            _fields_in_block(node.body, fields)
            break
    else:
        raise SystemExit(f"no `class Settings` found in {config_path}")
    return fields


def env_example_keys(env_example_path: Path) -> set[str]:
    keys = set()
    for line in env_example_path.read_text().splitlines():
        m = re.match(r"^#?([A-Z][A-Z0-9_]*)=", line)
        if m:
            keys.add(m.group(1))
    return keys


def compose_service_env(compose_path: Path, service: str) -> dict[str, str]:
    """Key -> raw value text for everything under `<service>:`'s
    `environment:` block. Regex-based rather than a real YAML parse, to
    stay dependency-free - relies on this project's two-space indentation
    convention (verified against docker-compose.yml, not assumed)."""
    text = compose_path.read_text()
    lines = text.splitlines()
    service_line = re.compile(rf"^  {re.escape(service)}:\s*$")
    start = None
    for i, line in enumerate(lines):
        if service_line.match(line):
            start = i
            break
    if start is None:
        raise SystemExit(f"service `{service}` not found in {compose_path}")

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(r"^  \S", lines[i]):  # next top-level service or key
            end = i
            break

    env_start = None
    for i in range(start + 1, end):
        if re.match(r"^    environment:\s*$", lines[i]):
            env_start = i
            break
    if env_start is None:
        return {}

    env: dict[str, str] = {}
    for i in range(env_start + 1, end):
        line = lines[i]
        if not line.strip():
            continue  # blank line, still inside the block
        if re.match(r"^      #", line):
            continue  # comment at environment-block indent, e.g. the OSM_DIR note
        m = re.match(r"^      ([A-Za-z][A-Za-z0-9_]*):\s*(.*)$", line)
        if not m:
            break  # dedented out of the environment block
        env[m.group(1)] = m.group(2)
    return env


def check(
    label: str,
    config_path: Path,
    compose_services: list[str],
    no_env_example: set[str],
    no_compose: set[str],
) -> list[str]:
    problems = []
    fields = settings_fields(config_path)
    env_keys = env_example_keys(REPO_ROOT / ".env.example")

    for field in sorted(fields - no_env_example):
        env_name = field.upper()
        if env_name not in env_keys:
            problems.append(
                f"{label}: `{field}` (Settings field in {config_path.relative_to(REPO_ROOT)}) "
                f"has no {env_name}= entry in .env.example"
            )

    compose_path = REPO_ROOT / "docker-compose.yml"
    for service in compose_services:
        service_env = compose_service_env(compose_path, service)
        for field in sorted(fields - no_compose):
            env_name = field.upper()
            if env_name not in service_env:
                problems.append(
                    f"{label}: `{field}` (Settings field in {config_path.relative_to(REPO_ROOT)}) "
                    f"is not in the `{service}` environment block of docker-compose.yml"
                )
                continue
            # The VALHALLA_URL shape of this bug family: the key exists, but
            # its value is a hardcoded literal rather than ${ENV_NAME:-...},
            # so setting the .env variable has no effect at all even though
            # both the key and a .env.example entry exist.
            value = service_env[env_name]
            if f"${{{env_name}" not in value:
                problems.append(
                    f"{label}: `{env_name}` in the `{service}` environment block of "
                    f"docker-compose.yml is a hardcoded literal ({value!r}), not "
                    f"${{{env_name}:-...}} - setting it in .env has no effect"
                )
    return problems


def check_env_example_has_no_orphans(
    backend_fields: set[str], indexer_fields: set[str]
) -> list[str]:
    known = {f.upper() for f in backend_fields | indexer_fields} | ENV_EXAMPLE_NON_SETTINGS_KEYS
    problems = []
    for key in sorted(env_example_keys(REPO_ROOT / ".env.example") - known):
        problems.append(
            f".env.example: `{key}` is not a backend or indexer Settings field, and is not in "
            "ENV_EXAMPLE_NON_SETTINGS_KEYS in this script - either nothing reads it, or the "
            "allowlist needs updating"
        )
    return problems


def main() -> int:
    backend_config = REPO_ROOT / "backend" / "app" / "config.py"
    indexer_config = REPO_ROOT / "indexer" / "indexer" / "config.py"

    problems = []
    problems += check(
        "backend",
        backend_config,
        BACKEND_COMPOSE_SERVICES,
        BACKEND_NO_ENV_EXAMPLE,
        BACKEND_NO_COMPOSE,
    )
    problems += check(
        "indexer",
        indexer_config,
        INDEXER_COMPOSE_SERVICES,
        INDEXER_NO_ENV_EXAMPLE,
        INDEXER_NO_COMPOSE,
    )
    problems += check_env_example_has_no_orphans(
        settings_fields(backend_config), settings_fields(indexer_config)
    )

    if problems:
        print("Config surface mismatch(es) found:\n", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print(
            "\nEvery config field needs to be readable from .env AND actually wired "
            "through compose, or explicitly allowlisted in deploy/check-config-surface.py "
            "with a reason. See the module docstring.",
            file=sys.stderr,
        )
        return 1

    print("Config surface OK: every Settings field is documented and wired through compose.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
