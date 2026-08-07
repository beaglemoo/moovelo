/** Colours a route by gradient - the elevation chart and the map line share
 * the same bands and the same point-to-point maths, so a red stretch on the
 * chart is the same red stretch on the map. */

import type { ElevationPoint } from './api';
import { pointAtDistance } from './geo';

export interface GradientBand {
	max: number;
	colour: string;
	label: string;
}

export const GRADIENT_BANDS: GradientBand[] = [
	{ max: 0, colour: '#268bd2', label: 'Descent' },
	{ max: 3, colour: '#859900', label: '0-3%' },
	{ max: 6, colour: '#b58900', label: '3-6%' },
	{ max: 9, colour: '#cb4b16', label: '6-9%' },
	{ max: 12, colour: '#dc322f', label: '9-12%' },
	{ max: Infinity, colour: '#6c1a1a', label: '12%+' }
];

/** Index into GRADIENT_BANDS for a grade percentage. */
export function bandIndex(gradePct: number): number {
	// Strictly negative only: a perfectly flat step (grade exactly 0, the
	// normal case on flat ground) belongs in 0-3%, not "Descent".
	if (gradePct < 0) return 0;
	for (let i = 1; i < GRADIENT_BANDS.length; i++) {
		if (gradePct <= GRADIENT_BANDS[i].max) return i;
	}
	return GRADIENT_BANDS.length - 1;
}

export function bandColour(gradePct: number): string {
	return GRADIENT_BANDS[bandIndex(gradePct)].colour;
}

export interface GradientSegment {
	/** [lon, lat] coordinates, in route order. */
	coords: [number, number][];
	band: number;
}

/**
 * One band per point-to-point step between consecutive elevation samples.
 *
 * Grade is (elev delta / dist delta) * 100, no smoothing, because this is a
 * coarse visual ("roughly how steep was that stretch") rather than climb
 * detection. Shared by the map line and the elevation chart, which colour
 * the same route and must agree on where the bands fall.
 */
export function stepBands(elevation: ElevationPoint[]): number[] {
	const bands: number[] = [];
	for (let i = 1; i < elevation.length; i++) {
		const distDelta = elevation[i].dist_m - elevation[i - 1].dist_m;
		const elevDelta = elevation[i].elev_m - elevation[i - 1].elev_m;
		const gradePct = distDelta > 0 ? (elevDelta / distDelta) * 100 : 0;
		bands.push(bandIndex(gradePct));
	}
	return bands;
}

/** Merges consecutive same-band steps into runs over an elevation index range. */
function mergeRuns(bands: number[]): { band: number; startIdx: number; endIdx: number }[] {
	const runs: { band: number; startIdx: number; endIdx: number }[] = [];
	let runStart = 0;
	for (let i = 0; i < bands.length; i++) {
		if (i === bands.length - 1 || bands[i + 1] !== bands[i]) {
			runs.push({ band: bands[i], startIdx: runStart, endIdx: i + 1 });
			runStart = i + 1;
		}
	}
	return runs;
}

/**
 * Splits a route into gradient-coloured segments for the map line.
 *
 * Each run's distance range is mapped onto the route line coordinates by
 * interpolating against `routeDists`, including the interpolated boundary
 * points so adjacent segments share an endpoint and the coloured line has
 * no gaps.
 */
export function gradientSegments(
	elevation: ElevationPoint[],
	routeLine: [number, number][],
	routeDists: number[]
): GradientSegment[] {
	if (elevation.length < 2 || routeLine.length < 2) return [];

	const runs = mergeRuns(stepBands(elevation));

	return runs
		.map((run) => ({
			band: run.band,
			coords: coordsBetween(
				routeLine,
				routeDists,
				elevation[run.startIdx].dist_m,
				elevation[run.endIdx].dist_m
			)
		}))
		.filter((segment) => segment.coords.length >= 2);
}

/** Runs of consecutive elevation points sharing a band, indices into `elevation`. */
export function elevationRuns(
	elevation: ElevationPoint[]
): { band: number; startIdx: number; endIdx: number }[] {
	if (elevation.length < 2) return [];
	return mergeRuns(stepBands(elevation));
}

function coordsBetween(
	routeLine: [number, number][],
	routeDists: number[],
	distStart: number,
	distEnd: number
): [number, number][] {
	const coords: [number, number][] = [];
	const start = pointAtDistance(routeLine, routeDists, distStart);
	if (start) coords.push(start);
	for (let j = 0; j < routeLine.length; j++) {
		if (routeDists[j] > distStart && routeDists[j] < distEnd) coords.push(routeLine[j]);
	}
	const end = pointAtDistance(routeLine, routeDists, distEnd);
	if (end) coords.push(end);
	return coords;
}
