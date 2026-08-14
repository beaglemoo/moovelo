/** Small geometry helpers over [lon, lat] coordinate arrays. */

const EARTH_RADIUS_M = 6371000;

export function haversine(a: [number, number], b: [number, number]): number {
	const dLat = ((b[1] - a[1]) * Math.PI) / 180;
	const dLon = ((b[0] - a[0]) * Math.PI) / 180;
	const lat1 = (a[1] * Math.PI) / 180;
	const lat2 = (b[1] * Math.PI) / 180;
	const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
	return 2 * EARTH_RADIUS_M * Math.asin(Math.sqrt(h));
}

/** Cumulative distance (meters) along a line for each vertex. */
export function cumulativeDistances(line: [number, number][]): number[] {
	const dists: number[] = [0];
	for (let i = 1; i < line.length; i++) {
		dists.push(dists[i - 1] + haversine(line[i - 1], line[i]));
	}
	return dists;
}

/** Coordinate at a given distance along the line, linearly interpolated. */
export function pointAtDistance(
	line: [number, number][],
	dists: number[],
	target: number
): [number, number] | null {
	if (line.length === 0) return null;
	if (target <= 0) return line[0];
	const total = dists[dists.length - 1];
	if (target >= total) return line[line.length - 1];
	let lo = 0;
	let hi = dists.length - 1;
	while (lo + 1 < hi) {
		const mid = (lo + hi) >> 1;
		if (dists[mid] <= target) lo = mid;
		else hi = mid;
	}
	const span = dists[hi] - dists[lo];
	const t = span > 0 ? (target - dists[lo]) / span : 0;
	return [
		line[lo][0] + (line[hi][0] - line[lo][0]) * t,
		line[lo][1] + (line[hi][1] - line[lo][1]) * t
	];
}

/** Merge a route's per-leg decoded lines into one, dropping a leg's first
 * point when it duplicates the previous leg's last point - the join `+page
 * .svelte`'s own `routeLine` derivation uses, exported here so a page that
 * only ever reads a route (never edits it) does not need to keep its own
 * copy. */
export function mergeLegLines(legs: [number, number][][]): [number, number][] {
	const merged: [number, number][] = [];
	for (const leg of legs) {
		const start =
			merged.length &&
			leg.length &&
			merged[merged.length - 1][0] === leg[0][0] &&
			merged[merged.length - 1][1] === leg[0][1]
				? 1
				: 0;
		merged.push(...leg.slice(start));
	}
	return merged;
}

/** Index of the vertex nearest to a point (planar approximation is fine here). */
export function nearestVertexIndex(line: [number, number][], point: [number, number]): number {
	let best = 0;
	let bestDist = Infinity;
	const cosLat = Math.cos((point[1] * Math.PI) / 180);
	for (let i = 0; i < line.length; i++) {
		const dx = (line[i][0] - point[0]) * cosLat;
		const dy = line[i][1] - point[1];
		const d = dx * dx + dy * dy;
		if (d < bestDist) {
			bestDist = d;
			best = i;
		}
	}
	return best;
}
