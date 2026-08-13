/**
 * Shared display formatting, so distances read the same everywhere and honour
 * the rider's metric/imperial choice. Every function takes the unit system
 * explicitly (from the `units` store) rather than reading a global, so it stays
 * pure and testable; the backend and stored values are always metric.
 */
export type UnitSystem = 'metric' | 'imperial';

export const KM_PER_MILE = 1.609344;
export const METRES_PER_FOOT = 0.3048;

/** Long distances (route length): one decimal, km or mi. */
export function distance(metres: number, units: UnitSystem): string {
	const value = units === 'imperial' ? metres / 1000 / KM_PER_MILE : metres / 1000;
	return `${value.toFixed(1)} ${units === 'imperial' ? 'mi' : 'km'}`;
}

/**
 * Signed distance delta, e.g. "+3.2 km" / "-1.0 mi".
 *
 * Formats the signed value itself (via `distance`), rather than rounding the
 * absolute value and re-applying the sign, for consistency with
 * `elevationDelta` below - same shape of fix, same reason (see there for why
 * an abs+sign round-trip disagrees with rounding the signed value once
 * Math.round is involved).
 *
 * toFixed does not collapse negative zero, so a small negative delta can
 * still come back as "-0.0 mi" (e.g. distanceDelta(-60, 'imperial')); the
 * sign is stripped only when the *displayed* magnitude is zero.
 */
export function distanceDelta(metres: number, units: UnitSystem): string {
	const body = distance(metres, units);
	const magnitude = Math.abs(parseFloat(body));
	if (magnitude === 0) return body.replace(/^-/, '');
	return metres > 0 ? `+${body}` : body;
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

/**
 * Signed elevation delta, e.g. "+40 m" / "-131 ft".
 *
 * Formats the signed value itself (via `elevation`, i.e. plain Math.round),
 * rather than rounding the absolute value and re-applying the sign - the two
 * disagree for negatives ending in .5, where Math.round rounds towards
 * +Infinity: elevationDelta(-1.5, 'metric') === '-1 m' (Math.round(-1.5) is
 * -1), where the old abs+sign approach gave '-2 m'.
 * elevationDelta(-0.4, 'metric') === '0 m' (Math.round(-0.4) is -0, and
 * String(-0) has no sign, so no stripping is even needed there).
 */
export function elevationDelta(metres: number, units: UnitSystem): string {
	const body = elevation(metres, units);
	const magnitude = Math.abs(parseInt(body, 10));
	if (magnitude === 0) return body.replace(/^-/, '');
	return metres > 0 ? `+${body}` : body;
}

/** Speed given in km/h, shown as km/h or mph. */
export function speed(kmh: number, units: UnitSystem): string {
	const value = units === 'imperial' ? kmh / KM_PER_MILE : kmh;
	return `${Math.round(value)} ${units === 'imperial' ? 'mph' : 'km/h'}`;
}
