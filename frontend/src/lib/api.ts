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

export interface AuthStatus {
	setup_required: boolean;
	signups_enabled: boolean;
	oidc: { enabled: boolean; name: string | null };
}

export interface UserInfo {
	email: string;
	is_admin: boolean;
}

export interface RouteSummary {
	id: string;
	name: string;
	preset: Preset;
	distance_m: number;
	ascent_m: number;
	updated_at: string;
}

export interface SavedRoute extends RouteResponse {
	id: string;
	name: string;
	preset: Preset;
	waypoints: Waypoint[];
	updated_at: string;
}

export class ApiError extends Error {
	constructor(
		public status: number,
		message: string
	) {
		super(message);
	}
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
	const response = await fetch(path, {
		headers: init?.body ? { 'Content-Type': 'application/json' } : undefined,
		...init
	});
	if (!response.ok) {
		let detail = `Request failed (${response.status})`;
		try {
			const body = await response.json();
			if (typeof body.detail === 'string') detail = body.detail;
		} catch {
			// keep the generic message
		}
		throw new ApiError(response.status, detail);
	}
	if (response.status === 204) return undefined as T;
	return response.json();
}

export const auth = {
	status: () => request<AuthStatus>('/api/auth/status'),
	me: () => request<UserInfo>('/api/auth/me'),
	register: (email: string, password: string) =>
		request<UserInfo>('/api/auth/register', {
			method: 'POST',
			body: JSON.stringify({ email, password })
		}),
	login: (email: string, password: string) =>
		request<UserInfo>('/api/auth/login', {
			method: 'POST',
			body: JSON.stringify({ email, password })
		}),
	logout: () => request<{ status: string }>('/api/auth/logout', { method: 'POST' })
};

export interface RoutePayload {
	name: string;
	waypoints: Waypoint[];
	preset: Preset;
	snapshot: RouteResponse;
}

export const routes = {
	list: () => request<RouteSummary[]>('/api/routes'),
	get: (id: string) => request<SavedRoute>(`/api/routes/${id}`),
	create: (payload: RoutePayload) =>
		request<SavedRoute>('/api/routes', { method: 'POST', body: JSON.stringify(payload) }),
	update: (id: string, payload: Partial<RoutePayload>) =>
		request<SavedRoute>(`/api/routes/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
	remove: (id: string) => request<void>(`/api/routes/${id}`, { method: 'DELETE' }),
	gpxUrl: (id: string) => `/api/routes/${id}/export.gpx`,
	fitUrl: (id: string) => `/api/routes/${id}/export.fit`
};

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
