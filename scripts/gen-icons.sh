#!/usr/bin/env bash
# Rasterise the PWA icon set from the SVG sources in frontend/static/.
#
# The PNGs are committed so the build needs no image toolchain; run this only
# when the SVG sources change (e.g. a real logo replaces the placeholder
# monogram). Requires rsvg-convert (`brew install librsvg` /
# `apt-get install librsvg2-bin`).
#
# Usage: scripts/gen-icons.sh
set -euo pipefail

cd "$(dirname "$0")/.."
STATIC=frontend/static

command -v rsvg-convert >/dev/null || {
	echo "error: rsvg-convert not found (brew install librsvg / apt-get install librsvg2-bin)" >&2
	exit 1
}

render() {
	local src="$1" size="$2" out="$3"
	rsvg-convert -w "$size" -h "$size" "$STATIC/$src" -o "$STATIC/$out"
	echo "  $out (${size}x${size})"
}

echo "Rendering icons from $STATIC/*.svg:"
render icon.svg 192 icon-192.png
render icon.svg 512 icon-512.png
render icon.svg 32 favicon-32.png
render icon-maskable.svg 512 icon-512-maskable.png
# Apple touch icons must be opaque and square; iOS applies its own corner mask,
# so the full-bleed maskable source is the right base.
render icon-maskable.svg 180 apple-touch-icon.png
echo "Done."
