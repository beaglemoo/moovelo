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

if [ "$(git describe --tags --exact-match 2>/dev/null)" != "$VERSION" ]; then
	echo "warning: HEAD is not at tag $VERSION ($(git describe --tags --always))" >&2
	read -rp "continue anyway? [y/N] " yn
	[ "$yn" = "y" ] || exit 1
fi

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
