"""Shrinking the extract before Python ever sees it.

England is a 1.6 GB extract with tens of millions of nodes, and the
staging machine has 12 GB of RAM shared with Valhalla and Postgres.
Handing that whole file to pyosmium with node-location caching would be
the single largest memory consumer on the box, to resolve the handful of
objects that actually matter.

`osmium tags-filter` is a C++ streaming pass that keeps only ID tables in
memory. It reduces the file to matching objects plus the nodes and members
they reference, which is what makes the pyosmium passes that follow cheap.
"""

import logging
import shutil
import subprocess
from pathlib import Path

from indexer.categories import filter_expressions
from indexer.config import settings

log = logging.getLogger(__name__)


class FilterError(RuntimeError):
    pass


def find_extracts(osm_dir: str | None = None) -> list[Path]:
    """Every .osm.pbf Valhalla downloaded.

    VALHALLA_TILE_URL takes a space-separated list, so more than one is
    legitimate and all of them have to be indexed.
    """
    directory = Path(osm_dir or settings.osm_dir)
    if not directory.is_dir():
        raise FilterError(f"{directory} is not a directory - is the tiles volume mounted?")
    return sorted(directory.glob("*.osm.pbf"))


def filter_extract(source: Path, destination: Path) -> Path:
    """Run tags-filter over one extract, returning the filtered file.

    Note there is no -R. That flag *omits* referenced objects, which reads
    like the opposite of what it does: referenced nodes and relation
    members are included by default, and that inclusion is exactly what
    gives ways their coordinates and cycle relations their geometry. Adding
    -R here would produce an index full of objects with no location, and it
    would fail as empty results rather than as an error.
    """
    if shutil.which(settings.osmium_binary) is None:
        raise FilterError(
            f"{settings.osmium_binary} not found - the indexer image installs osmium-tool"
        )
    # The extract lives on a read-only mount, so the output cannot sit
    # beside it.
    destination.parent.mkdir(parents=True, exist_ok=True)

    command = [
        settings.osmium_binary,
        "tags-filter",
        "--overwrite",
        # Drop tags from objects kept only to complete a reference, which
        # shrinks the output without losing any geometry.
        "--remove-tags",
        "--output",
        str(destination),
        str(source),
        *filter_expressions(settings.index_roads),
    ]
    log.info("filtering %s -> %s", source.name, destination.name)
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise FilterError(f"osmium tags-filter failed on {source.name}: {result.stderr.strip()}")

    log.info(
        "filtered %s: %.0f MB -> %.0f MB",
        source.name,
        source.stat().st_size / 1e6,
        destination.stat().st_size / 1e6,
    )
    return destination
