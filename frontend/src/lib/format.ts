/**
 * Shared display formatting, so distances read the same everywhere and honour
 * the rider's metric/imperial choice. Every function takes the unit system
 * explicitly (from the `units` store) rather than reading a global, so it stays
 * pure and testable; the backend and stored values are always metric.
 */
export type UnitSystem = 'metric' | 'imperial';

const KM_PER_MILE = 1.609344;
export const METRES_PER_FOOT = 0.3048;

/** Long distances (route length): one decimal, km or mi. */
export function distance(metres: number, units: UnitSystem): string {
	const value = units === 'imperial' ? metres / 1000 / KM_PER_MILE : metres / 1000;
	return `${value.toFixed(1)} ${units === 'imperial' ? 'mi' : 'km'}`;
}

/** Signed distance delta, e.g. "+3.2 km" / "-1.0 mi". */
export function distanceDelta(metres: number, units: UnitSystem): string {
	// Sign the rounded magnitude, not the raw metres: toFixed does not collapse
	// negative zero, so a -60 m delta in miles would otherwise read "-0.0 mi".
	const body = distance(Math.abs(metres), units);
	const sign = parseFloat(body) === 0 ? '' : metres < 0 ? '-' : '+';
	return `${sign}${body}`;
}

/**
 * Short distances (how far along a route, how far away a place is): metres or
 * feet close in, switching to km or mi past a threshold.
 */
export function shortLength(metres: number, units: UnitSystem): string {
	if (units === 'imperial') {
		const feet = metres / METRES_PER_FOOT;
		return feet < 1000
			? `${Math.round(feet)} ft`
			: `${(metres / 1000 / KM_PER_MILE).toFixed(1)} mi`;
	}
	return metres < 1000 ? `${Math.round(metres)} m` : `${(metres / 1000).toFixed(1)} km`;
}

/** Elevation / ascent, rounded to whole units: m or ft. */
export function elevation(metres: number, units: UnitSystem): string {
	const value = units === 'imperial' ? metres / METRES_PER_FOOT : metres;
	return `${Math.round(value)} ${units === 'imperial' ? 'ft' : 'm'}`;
}

/** Signed elevation delta, e.g. "+40 m" / "-131 ft". */
export function elevationDelta(metres: number, units: UnitSystem): string {
	const body = elevation(Math.abs(metres), units);
	const sign = parseInt(body, 10) === 0 ? '' : metres < 0 ? '-' : '+';
	return `${sign}${body}`;
}

/** Speed given in km/h, shown as km/h or mph. */
export function speed(kmh: number, units: UnitSystem): string {
	const value = units === 'imperial' ? kmh / KM_PER_MILE : kmh;
	return `${Math.round(value)} ${units === 'imperial' ? 'mph' : 'km/h'}`;
}
