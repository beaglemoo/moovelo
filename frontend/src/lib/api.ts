export type Preset = 'road' | 'gravel' | 'quiet';

// Imported routes keep the track that was uploaded; their waypoints are only
// the endpoints, so re-routing one would discard the imported line.
export type RouteSource = 'planned' | 'imported';

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
	/** False until the place index has been built, which is opt-in. Gates
	 * the search box, the POI panel and the cycle-network overlay. */
	search_enabled: boolean;
	/** Epoch seconds of the last index build, or null. Used to version the
	 * cycle-network tile URL so a re-index is not hidden behind a day of
	 * browser cache. */
	search_index_version: string | null;
}

const CONFIG_FALLBACK: AppConfig = {
	tile_url_cyclosm: null,
	search_enabled: false,
	search_index_version: null
};

export interface PlaceResult {
	id: number;
	name: string;
	/** city | town | village | hamlet | suburb | locality | peak | station */
	place_type: string;
	lat: number;
	lon: number;
	/** Metres from the map centre, when one was sent. Shown because many
	 * English places share a name, and distance is what tells them apart. */
	distance_m: number | null;
}

export interface PoiResult {
	id: number;
	/** Null for the unnamed majority - a drinking fountain, a repair stand -
	 * which are exactly the ones worth finding. Render the category instead. */
	name: string | null;
	category: string;
	lat: number;
	lon: number;
	dist_from_route_m: number;
	/** Metres from the start, measured along the route. */
	dist_along_m: number;
	/** opening_hours, website and the like, straight from OSM. Untrusted
	 * text: render it, never interpret it. */
	tags: Record<string, string>;
}

export interface PoisAlongRoute {
	pois: PoiResult[];
	truncated: boolean;
}

export interface AuthStatus {
	setup_required: boolean;
	signups_enabled: boolean;
	password_login: boolean;
	oidc: { enabled: boolean; name: string | null };
}

export interface AdminUser {
	id: string;
	email: string;
	is_admin: boolean;
	created_at: string;
	route_count: number;
	wahoo_connected: boolean;
}

export interface AdminOverview {
	users: AdminUser[];
	stats: { user_count: number; route_count: number };
	config: {
		signups_enabled: boolean;
		password_login: boolean;
		oidc_enabled: boolean;
		oidc_provider: string | null;
		wahoo_configured: boolean;
	};
}

export interface UserInfo {
	email: string;
	is_admin: boolean;
}

export interface WahooState {
	status: 'none' | 'queued' | 'pushing' | 'synced' | 'error';
	error: string | null;
	route_id: string | null;
	pushed_at: string | null;
}

export interface WahooStatus {
	configured: boolean;
	connected: boolean;
	athlete: { name: string } | null;
}

export interface RouteSummary {
	id: string;
	name: string;
	preset: Preset;
	source: RouteSource;
	tags: string[];
	is_favourite: boolean;
	distance_m: number;
	ascent_m: number;
	updated_at: string;
	wahoo: WahooState;
	share_token: string | null;
}

export interface SavedRoute extends RouteResponse {
	id: string;
	name: string;
	preset: Preset;
	source: RouteSource;
	tags: string[];
	notes: string | null;
	is_favourite: boolean;
	waypoints: Waypoint[];
	updated_at: string;
	wahoo: WahooState;
	share_token: string | null;
}

export interface SharedRoute extends RouteResponse {
	name: string;
	preset: Preset;
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
		headers: typeof init?.body === 'string' ? { 'Content-Type': 'application/json' } : undefined,
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

export interface RouteMetadata {
	tags: string[];
	notes: string | null;
	is_favourite: boolean;
}

export interface RoutePayload {
	name: string;
	waypoints: Waypoint[];
	preset: Preset;
	snapshot: RouteResponse;
}

export interface RouteQuery {
	q?: string;
	tag?: string;
	favourite?: boolean;
	source?: RouteSource;
	sort?: 'updated' | 'name' | 'distance' | 'ascent';
	order?: 'asc' | 'desc';
}

export const routes = {
	list: (query: RouteQuery = {}) => {
		const params = new URLSearchParams();
		for (const [key, value] of Object.entries(query)) {
			if (value !== undefined && value !== '') params.set(key, String(value));
		}
		const qs = params.toString();
		return request<RouteSummary[]>(`/api/routes${qs ? `?${qs}` : ''}`);
	},
	tags: () => request<string[]>('/api/routes/tags'),
	get: (id: string) => request<SavedRoute>(`/api/routes/${id}`),
	create: (payload: RoutePayload) =>
		request<SavedRoute>('/api/routes', { method: 'POST', body: JSON.stringify(payload) }),
	update: (id: string, payload: Partial<RoutePayload> & Partial<RouteMetadata>) =>
		request<SavedRoute>(`/api/routes/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
	remove: (id: string) => request<void>(`/api/routes/${id}`, { method: 'DELETE' }),
	duplicate: (id: string) => request<SavedRoute>(`/api/routes/${id}/duplicate`, { method: 'POST' }),
	reverse: (id: string) => request<SavedRoute>(`/api/routes/${id}/reverse`, { method: 'POST' }),
	importFile: (file: File, preset: Preset) => {
		const form = new FormData();
		form.append('file', file);
		form.append('preset', preset);
		return request<SavedRoute>('/api/routes/import', { method: 'POST', body: form });
	},
	gpxUrl: (id: string) => `/api/routes/${id}/export.gpx`,
	fitUrl: (id: string) => `/api/routes/${id}/export.fit`,
	share: (id: string) => request<SavedRoute>(`/api/routes/${id}/share`, { method: 'POST' }),
	revokeShare: (id: string) => request<SavedRoute>(`/api/routes/${id}/share`, { method: 'DELETE' })
};

export const shared = {
	get: (token: string) => request<SharedRoute>(`/api/shared/${token}`),
	gpxUrl: (token: string) => `/api/shared/${token}/export.gpx`
};

export const admin = {
	overview: () => request<AdminOverview>('/api/admin/overview'),
	deleteUser: (id: string) => request<void>(`/api/admin/users/${id}`, { method: 'DELETE' })
};

export const wahoo = {
	status: () => request<WahooStatus>('/api/wahoo/status'),
	disconnect: () => request<{ status: string }>('/api/wahoo/disconnect', { method: 'POST' }),
	push: (routeId: string) =>
		request<RouteSummary>(`/api/wahoo/push/${routeId}`, { method: 'POST' }),
	connectUrl: '/api/wahoo/connect'
};

export const places = {
	/** Search the offline place index. Takes an AbortSignal because a
	 * combobox supersedes its own request on every keystroke, so `request()`
	 * - which has no way to pass one - is not usable here. */
	search: async (
		q: string,
		near?: { lat: number; lon: number },
		signal?: AbortSignal
	): Promise<PlaceResult[]> => {
		const params = new URLSearchParams({ q });
		if (near) {
			params.set('near_lat', String(near.lat));
			params.set('near_lon', String(near.lon));
		}
		const response = await fetch(`/api/places/search?${params}`, { signal });
		if (!response.ok) throw new ApiError(response.status, 'Place search failed');
		return await response.json();
	},

	/** Name a point from the offline index. Resolves to null when nothing is
	 * near it, when the index is not built, and when the lookup fails - every
	 * caller is decorating something that has to work without a name. */
	/** POIs within `radiusM` of a route line, ordered along it. Takes a
	 * signal because changing a category refetches, and the route can change
	 * underneath a request that is still in flight. */
	poisAlongRoute: async (
		line: [number, number][],
		categories: string[],
		radiusM: number,
		signal?: AbortSignal
	): Promise<PoisAlongRoute> => {
		const response = await fetch('/api/places/pois-along-route', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ line, categories, radius_m: radiusM }),
			signal
		});
		if (!response.ok) throw new ApiError(response.status, 'Could not look up points of interest');
		return await response.json();
	},

	reverse: async (lat: number, lon: number): Promise<PlaceResult | null> => {
		const params = new URLSearchParams({ lat: String(lat), lon: String(lon) });
		try {
			const response = await fetch(`/api/places/reverse?${params}`);
			if (!response.ok) return null;
			return await response.json();
		} catch {
			return null;
		}
	}
};

export async function fetchConfig(): Promise<AppConfig> {
	try {
		const response = await fetch('/api/config');
		if (!response.ok) return CONFIG_FALLBACK;
		return await response.json();
	} catch {
		return CONFIG_FALLBACK;
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
