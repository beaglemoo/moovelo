export type Preset = 'road' | 'gravel' | 'quiet';

export interface Waypoint {
	lat: number;
	lon: number;
}

export interface ElevationPoint {
	dist_m: number;
	elev_m: number;
}

export interface Maneuver {
	type: number;
	instruction: string;
	begin_shape_index?: number;
	[key: string]: unknown;
}

export interface RouteLeg {
	geometry: string;
	maneuvers: Maneuver[];
}

export interface RouteResponse {
	legs: RouteLeg[];
	distance_m: number;
	duration_s: number;
	ascent_m: number;
	descent_m: number;
	elevation: ElevationPoint[];
}

export interface AppConfig {
	tile_url_cyclosm: string | null;
}

export async function fetchConfig(): Promise<AppConfig> {
	try {
		const response = await fetch('/api/config');
		if (!response.ok) return { tile_url_cyclosm: null };
		return await response.json();
	} catch {
		return { tile_url_cyclosm: null };
	}
}

export async function planRoute(
	waypoints: Waypoint[],
	preset: Preset,
	signal?: AbortSignal
): Promise<RouteResponse> {
	const response = await fetch('/api/route', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ waypoints, preset }),
		signal
	});
	if (!response.ok) {
		let detail = `Routing failed (${response.status})`;
		try {
			const body = await response.json();
			if (typeof body.detail === 'string') detail = body.detail;
		} catch {
			// keep the generic message
		}
		throw new Error(detail);
	}
	return response.json();
}
