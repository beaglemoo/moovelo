"""Shared fixtures for indexer tests that need a real Postgres/PostGIS.

Only test_db.py needs this - test_extract.py runs entirely off a fixture
file, with no database involved. The throwaway-database dance mirrors
backend/tests/conftest.py's own `db_factory` fixture, built on psycopg
(sync) instead of SQLAlchemy since the indexer carries no ORM - db.py talks
to Postgres directly, and these fixtures do the same.
"""

import uuid
from collections.abc import Iterator

import psycopg
import pytest

TEST_DB_HOST = "127.0.0.1:5433"
ADMIN_URL = f"postgresql://bikegps:bikegps@{TEST_DB_HOST}/bikegps"

# A hand-written mirror of the columns backend migrations 0006 and 0014
# actually define for the tables db.py touches - not a full copy. Indexes
# beyond what db.py's own queries rely on (the natural-key uniqueness
# publish() dedupes against) are left out, the same way the indexer's own
# staging clones leave them out.
_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE places (
    id bigserial PRIMARY KEY,
    osm_type varchar(1) NOT NULL,
    osm_id bigint NOT NULL,
    name text NOT NULL,
    name_norm text NOT NULL,
    place_type text NOT NULL,
    county text,
    population integer,
    importance double precision NOT NULL DEFAULT 0,
    geog geography(Point, 4326) NOT NULL
);
CREATE UNIQUE INDEX ix_places_osm ON places (osm_type, osm_id);

CREATE TABLE pois (
    id bigserial PRIMARY KEY,
    osm_type varchar(1) NOT NULL,
    osm_id bigint NOT NULL,
    name text,
    name_norm text,
    category text NOT NULL,
    osm_tags jsonb NOT NULL DEFAULT '{}',
    geog geography(Point, 4326) NOT NULL
);
CREATE UNIQUE INDEX ix_pois_osm ON pois (osm_type, osm_id);

CREATE TABLE cycle_ways (
    id bigint PRIMARY KEY,
    ref text,
    name text,
    network text NOT NULL,
    operator text,
    geom geometry(MultiLineString, 4326) NOT NULL
);

CREATE TABLE cycle_way_members (
    id bigserial PRIMARY KEY,
    relation_id bigint NOT NULL,
    way_id bigint NOT NULL,
    length_m double precision NOT NULL,
    geom geometry(LineString, 4326) NOT NULL
);
CREATE UNIQUE INDEX ix_cycle_way_members_relation_way
    ON cycle_way_members (relation_id, way_id);
CREATE INDEX ix_cycle_way_members_geom ON cycle_way_members USING gist (geom);

CREATE TABLE osm_ways (
    way_id bigint PRIMARY KEY,
    highway text NOT NULL,
    name text,
    length_m double precision NOT NULL,
    geom geometry(LineString, 4326) NOT NULL
);
CREATE INDEX ix_osm_ways_geom ON osm_ways USING gist (geom);
CREATE INDEX ix_osm_ways_highway ON osm_ways (highway);

CREATE TABLE search_index_meta (
    id boolean PRIMARY KEY DEFAULT true CHECK (id),
    built_at timestamptz NOT NULL,
    source_files text[] NOT NULL,
    place_count integer NOT NULL,
    poi_count integer NOT NULL,
    cycle_way_count integer NOT NULL,
    cycle_way_member_count integer,
    osm_way_count integer
);
"""


@pytest.fixture
def database_url() -> Iterator[str]:
    """A throwaway Postgres database with the tables the indexer publishes
    into. Skips if postgres is not reachable, the same convention the
    backend's tests use for the same reason - CI and local dev both run the
    same compose stack this points at."""
    dbname = f"moovelo_indexer_test_{uuid.uuid4().hex[:8]}"
    try:
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(f'CREATE DATABASE "{dbname}"')
    except psycopg.OperationalError:
        pytest.skip("test postgres not reachable on 127.0.0.1:5433 (run the dev compose stack)")

    url = f"postgresql://bikegps:bikegps@{TEST_DB_HOST}/{dbname}"
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(_SCHEMA)

    yield url

    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE "{dbname}" WITH (FORCE)')
