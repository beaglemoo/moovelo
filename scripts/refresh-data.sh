#!/usr/bin/env bash
# Refresh the OSM routing data: wipe Valhalla's tiles, let it rebuild from a
# fresh Geofabrik extract, then rebuild the place index.
#
# Why a host script and not a compose sidecar: refreshing means deleting the
# in-use `valhalla_tiles` named volume, and a container cannot remove a volume
# mounted into another running container. The socket-mounted alternative would
# hand a container root-equivalent control of the host - too much for a monthly
# job in a self-hosted app. So this runs on the host, beside `docker compose`,
# and is scheduled with cron or a systemd timer (see docs/data.md).
#
# Usage: scripts/refresh-data.sh [--profile dev|prod] [--skip-index] [--dry-run]
#
# Honours the same environment as `docker compose` (reads ./.env): INDEX_ROADS,
# VALHALLA_TILE_URL, OSM_DIR, WORK_DIR all flow through unchanged. Set
# SMOKE_ROUTE="lat,lon;lat,lon" to point the final route probe at your extract
# (default is central London, valid for the England default extract).
set -euo pipefail

PROFILE=prod
SKIP_INDEX=0
DRY_RUN=0

while [ $# -gt 0 ]; do
	case "$1" in
		--profile) PROFILE="${2:?--profile needs an argument}"; shift 2 ;;
		--profile=*) PROFILE="${1#*=}"; shift ;;
		--skip-index) SKIP_INDEX=1; shift ;;
		--dry-run) DRY_RUN=1; shift ;;
		-h|--help) sed -n '2,17p' "$0"; exit 0 ;;
		*) echo "error: unknown argument: $1" >&2; exit 2 ;;
	esac
done

case "$PROFILE" in
	dev)  APP_SERVICE=backend-dev ;;
	prod) APP_SERVICE=backend ;;
	*) echo "error: --profile must be dev or prod (got: $PROFILE)" >&2; exit 2 ;;
esac

cd "$(dirname "$0")/.."

COMPOSE=(docker compose)

# Everything mutating goes through run(): in --dry-run it is printed, not
# executed, which is this script's test surface (a real refresh is 30+ minutes
# and needs Docker). Read-only probes call docker directly so dry-run still
# resolves real values where it safely can.
run() {
	if [ "$DRY_RUN" = 1 ]; then
		printf 'DRYRUN'
		printf ' %q' "$@"
		printf '\n'
	else
		"$@"
	fi
}

log() { printf '==> %s\n' "$*"; }

# Resolve the fully-qualified volume name from the rendered config rather than
# hardcoding the `moovelo_` project prefix - a checkout with
# COMPOSE_PROJECT_NAME set, or a renamed project, would otherwise wipe the
# wrong volume (or none).
VOLUME="$("${COMPOSE[@]}" config --format json 2>/dev/null | jq -r '.volumes.valhalla_tiles.name // empty' || true)"
if [ -z "$VOLUME" ]; then
	if [ "$DRY_RUN" = 1 ]; then
		VOLUME="<project>_valhalla_tiles"  # placeholder; dry-run may run without a resolvable stack
	else
		echo "error: could not resolve the valhalla_tiles volume from 'docker compose config'" >&2
		echo "       is Docker running and are you in the compose project root?" >&2
		exit 1
	fi
fi

# Guard: refuse unless the TARGET profile's own app container is up. The
# `valhalla` service and its tiles volume are shared by both profiles, so a
# project-wide "is anything running" check would happily let `--profile prod`
# (the default) wipe the tiles of a running *dev* stack and then fail to bind
# its port on the way back up. Checking the profile's app service ($backend /
# $backend-dev) means we only ever refresh the stack the caller actually named,
# and refuse when a different profile - or nothing - is up. Skipped in dry-run.
if [ "$DRY_RUN" != 1 ] && [ -z "$("${COMPOSE[@]}" ps -q "$APP_SERVICE" 2>/dev/null)" ]; then
	echo "error: the '$PROFILE' stack is not running ($APP_SERVICE is not up)." >&2
	echo "       Refusing to wipe tiles for a stack you did not name; if a" >&2
	echo "       different profile is running, re-run with that --profile." >&2
	exit 1
fi

# Single-instance lock. mkdir is atomic on every platform (flock is absent on
# macOS); two concurrent runs would each try to delete the volume mid-rebuild.
# A fixed path, not ${TMPDIR}: a manual run (TMPDIR set on macOS) and a cron run
# (TMPDIR unset -> /tmp) must resolve to the same lock, or they would not
# exclude each other.
LOCK="/tmp/moovelo-refresh-data.lock"
if [ "$DRY_RUN" != 1 ]; then
	if ! mkdir "$LOCK" 2>/dev/null; then
		echo "error: another refresh is running (lock: $LOCK); remove it if stale" >&2
		exit 1
	fi
	trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT
fi

# The value the indexer will actually receive - read from the rendered compose
# config, which merges ./.env, not from this shell's own environment (bash
# never sources .env, so ${INDEX_ROADS:-false} would log "false" while compose
# passed "true"). Falls back to false if config can't be read (e.g. dry-run
# without Docker).
INDEX_ROADS_EFFECTIVE="$("${COMPOSE[@]}" --profile index config --format json 2>/dev/null | jq -r '.services.indexer.environment.INDEX_ROADS // "false"' || echo false)"
[ -n "$INDEX_ROADS_EFFECTIVE" ] || INDEX_ROADS_EFFECTIVE=false

log "Profile:      $PROFILE"
log "Tiles volume: $VOLUME"
if [ "$SKIP_INDEX" = 1 ]; then
	log "Reindex:      skipped"
else
	log "Reindex:      yes (INDEX_ROADS=$INDEX_ROADS_EFFECTIVE)"
fi
[ "$DRY_RUN" = 1 ] && log "(dry run - no changes will be made)"

# 1. Stop Valhalla and remove its container so the volume is unmounted.
log "Stopping Valhalla"
run "${COMPOSE[@]}" stop valhalla
run "${COMPOSE[@]}" rm -f valhalla

# 2. Wipe the tiles volume - this deletes the extract, forcing a fresh download.
log "Removing tiles volume $VOLUME"
run docker volume rm "$VOLUME"

# 3. Bring the stack back; Valhalla re-downloads the extract and rebuilds tiles.
log "Starting stack (--profile $PROFILE)"
run "${COMPOSE[@]}" --profile "$PROFILE" up -d

# 4. Wait for the tile build to finish (England ~30 min). The app is up but
#    routing 502s until this passes.
log "Waiting for Valhalla to become healthy (can take ~30 min for England)"
# Bounded wait: a broken build (bad extract, full disk, crash loop) shows
# `unhealthy` forever, and an unbounded loop here would hold the lockfile
# indefinitely under cron with no alert. HEALTH_TIMEOUT (default 2h - England
# on a small VM is documented as "an hour or more") caps it; on timeout we
# fail, the EXIT trap releases the lock, and cron/systemd sees a non-zero exit.
wait_healthy() {
	local cid status waited=0 max="${HEALTH_TIMEOUT:-7200}"
	while :; do
		cid="$("${COMPOSE[@]}" ps -q valhalla)"
		[ -n "$cid" ] || { echo "error: valhalla container not found after 'up'" >&2; return 1; }
		status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null || echo unknown)"
		case "$status" in
			healthy) return 0 ;;
			none|unknown) echo "error: valhalla has no healthcheck status ($status)" >&2; return 1 ;;
		esac
		if [ "$waited" -ge "$max" ]; then
			echo "error: valhalla did not become healthy within ${max}s (last status: $status)" >&2
			echo "       check 'docker compose logs valhalla'; raise HEALTH_TIMEOUT if the build is just slow" >&2
			return 1
		fi
		sleep 15
		waited=$((waited + 15))
		if [ $((waited % 300)) -eq 0 ]; then
			log "  still building... (${waited}s elapsed, status=$status)"
		fi
	done
}
if [ "$DRY_RUN" = 1 ]; then
	echo "DRYRUN wait for 'docker compose ps -q valhalla' health == healthy"
else
	wait_healthy
fi

# 5. Rebuild the place index (unless skipped). Honours INDEX_ROADS from the
#    environment / .env, exactly as a manual run does.
if [ "$SKIP_INDEX" = 1 ]; then
	log "Skipping reindex (--skip-index)"
else
	log "Rebuilding place index (INDEX_ROADS=$INDEX_ROADS_EFFECTIVE)"
	run "${COMPOSE[@]}" --profile index run --rm indexer
fi

# 6. Smoke check: one bicycle route through Valhalla. Warning-only, because the
#    destructive work is already done and a non-default extract would not cover
#    the default coordinates - the hard gate is the healthcheck in step 4.
log "Smoke check: one bicycle route"
smoke_route() {
	local pair="${SMOKE_ROUTE:-51.5074,-0.1278;51.5155,-0.1420}"
	local a="${pair%%;*}" b="${pair##*;}"
	local lat1="${a%%,*}" lon1="${a##*,}"
	local lat2="${b%%,*}" lon2="${b##*,}"
	"${COMPOSE[@]}" exec -T valhalla curl -fsS http://localhost:8002/route \
		--data "{\"locations\":[{\"lat\":$lat1,\"lon\":$lon1},{\"lat\":$lat2,\"lon\":$lon2}],\"costing\":\"bicycle\"}" \
		>/dev/null
}
if [ "$DRY_RUN" = 1 ]; then
	echo "DRYRUN docker compose exec -T valhalla curl -fsS http://localhost:8002/route --data <bicycle route>"
elif smoke_route; then
	log "Routing is back."
else
	echo "warning: smoke route failed - if you changed the extract, set SMOKE_ROUTE; else check 'docker compose logs valhalla'" >&2
fi

log "Refresh complete."
