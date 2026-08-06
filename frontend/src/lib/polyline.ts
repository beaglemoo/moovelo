/** Decode a Valhalla encoded polyline (precision 6) to [lon, lat] pairs. */
export function decodePolyline6(encoded: string): [number, number][] {
	const coords: [number, number][] = [];
	let lat = 0;
	let lon = 0;
	let i = 0;
	while (i < encoded.length) {
		for (const isLon of [false, true]) {
			let shift = 0;
			let result = 0;
			let byte: number;
			do {
				byte = encoded.charCodeAt(i++) - 63;
				result |= (byte & 0x1f) << shift;
				shift += 5;
			} while (byte >= 0x20);
			const delta = result & 1 ? ~(result >> 1) : result >> 1;
			if (isLon) lon += delta;
			else lat += delta;
		}
		coords.push([lon / 1e6, lat / 1e6]);
	}
	return coords;
}
