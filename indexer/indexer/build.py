"""Entrypoint: filter, extract, load, swap.

Run with `docker compose --profile index run --rm indexer`.
"""

import logging
import sys
import time
from pathlib import Path

from indexer import db, extract, prefilter
from indexer.config import settings

log = logging.getLogger("indexer")


def build() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    started = time.monotonic()

    try:
        extracts = prefilter.find_extracts()
    except prefilter.FilterError as error:
        log.error("%s", error)
        return 1
    if not extracts:
        log.error(
            "no .osm.pbf found in %s - has Valhalla finished downloading? "
            "The extract is deleted when the tiles volume is wiped, so run "
            "this after a data refresh, not before.",
            settings.osm_dir,
        )
        return 1

    log.info("indexing %d extract(s): %s", len(extracts), ", ".join(e.name for e in extracts))

    with db.connect(settings.database_url) as connection:
        db.prepare_staging(connection)

        totals = {"places": 0, "pois": 0, "cycle_members": 0, "road_ways": 0}
        for source in extracts:
            filtered = Path(settings.work_dir) / f"{source.stem}.filtered.osm.pbf"
            try:
                prefilter.filter_extract(source, filtered)
                routes = extract.read_cycle_routes(filtered)
                counts = db.load(settings.database_url, extract.read_features(filtered, routes))
            except prefilter.FilterError as error:
                log.error("%s", error)
                return 1
            finally:
                filtered.unlink(missing_ok=True)
            for key, value in counts.items():
                totals[key] += value

        totals["cycle_ways"] = db.assemble_cycle_routes(connection)
        totals["osm_ways"] = db.assemble_road_ways(connection)
        db.publish(connection, [e.name for e in extracts], totals)

    log.info(
        "done in %.0fs: %d places, %d POIs, %d cycle routes, %d road ways",
        time.monotonic() - started,
        totals["places"],
        totals["pois"],
        totals["cycle_ways"],
        totals["osm_ways"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(build())
