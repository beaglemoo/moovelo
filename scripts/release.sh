#!/usr/bin/env bash
# Build and push the multi-arch Moovelo image (linux/amd64 + linux/arm64)
# from a local machine, without GitHub Actions.
#
# Prerequisites (once per machine):
#   docker login ghcr.io                     # PAT with write:packages
#   docker run --privileged --rm tonistiigi/binfmt --install amd64|arm64
#                                            # emulation for the non-native arch
#
# Usage: scripts/release.sh vX.Y.Z
# Run from a checkout of the tag you are releasing.
set -euo pipefail

IMAGE="${IMAGE:-ghcr.io/beaglemoo/moovelo}"
VERSION="${1:?usage: scripts/release.sh vX.Y.Z}"
[[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
	echo "error: version must look like v1.2.3 (got: $VERSION)" >&2
	exit 1
}
V="${VERSION#v}"
MINOR="${V%.*}"

cd "$(dirname "$0")/.."

# A hard failure, not a prompt. This script's last act is `--push`: it
# publishes to a public registry and moves `latest`, so "are you sure?" is
# the wrong shape of guard - anything running it non-interactively answers
# yes. Releasing means tagging first.
if [ "$(git describe --tags --exact-match 2>/dev/null)" != "$VERSION" ]; then
	echo "error: HEAD is not at tag $VERSION (at $(git describe --tags --always))" >&2
	echo "       tag the commit you intend to release, then re-run" >&2
	exit 1
fi

# Refuse to build a release whose version manifests disagree with the tag -
# a stale backend/pyproject.toml or frontend/package.json must never reach
# an image tagged $VERSION. The npm lockfile carries the version twice
# (top-level and packages[""]) and the two can drift independently; it sat
# at 0.1.0 for six releases before this check. Regenerate it with
# `npm install --package-lock-only` after bumping package.json.
#
# backend/uv.lock is the fifth, added after it was found sitting at 0.7.1
# while pyproject.toml said 0.8.1 - stale since v0.8.0 and missed because
# this list only knew about four. Regenerate it with `uv lock` from
# backend/. The recurring lesson is that the count keeps being wrong: it
# was "three manifests" until v0.7.1, four until now. Before adding a
# sixth, prefer a check that finds version-bearing files rather than one
# that lists them.
PY_V=$(grep -m1 '^version = ' backend/pyproject.toml | cut -d'"' -f2)
UV_LOCK_V=$(grep -A2 '^name = "moovelo-backend"$' backend/uv.lock |
	grep -m1 '^version = ' | cut -d'"' -f2)
FE_V=$(node -p "require('./frontend/package.json').version")
LOCK_TOP_V=$(node -p "require('./frontend/package-lock.json').version")
LOCK_PKG_V=$(node -p "require('./frontend/package-lock.json').packages[''].version")
for pair in "backend/pyproject.toml:$PY_V" "backend/uv.lock:$UV_LOCK_V" \
	"frontend/package.json:$FE_V" \
	"frontend/package-lock.json (version):$LOCK_TOP_V" \
	"frontend/package-lock.json (packages[\"\"].version):$LOCK_PKG_V"; do
	f="${pair%%:*}"
	v="${pair##*:}"
	[ "$v" = "$V" ] || {
		echo "error: $f says $v, releasing $VERSION" >&2
		exit 1
	}
done

# A docker-container builder is required for multi-platform + push.
docker buildx inspect moovelo-release >/dev/null 2>&1 ||
	docker buildx create --name moovelo-release --driver docker-container
docker buildx use moovelo-release

exec docker buildx build \
	--platform linux/amd64,linux/arm64 \
	--file backend/Dockerfile --target prod \
	--provenance=false \
	--tag "$IMAGE:$V" \
	--tag "$IMAGE:$MINOR" \
	--tag "$IMAGE:latest" \
	--push .
