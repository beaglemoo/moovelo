/**
 * Pick black or white text for a coloured fill, whichever has more WCAG
 * contrast. Used for badges/chips/buttons whose background is one of the
 * fixed semantic colours (POI groups, climb categories, success/warning) -
 * those mid-tone colours fail AA with a single hardcoded white or black text,
 * so choose per fill. 0.179 is the relative-luminance crossover where white
 * and black give equal contrast; every semantic fill in the app clears 4.5:1
 * on the chosen side (verified against pois.ts / climbs.ts).
 */
export function readableText(fill: string): '#000000' | '#ffffff' {
	const h = fill.replace('#', '');
	const lin = [0, 2, 4]
		.map((i) => parseInt(h.slice(i, i + 2), 16) / 255)
		.map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
	const luminance = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
	return luminance > 0.179 ? '#000000' : '#ffffff';
}
