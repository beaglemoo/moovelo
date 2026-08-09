import type { Feature } from 'geojson';

export type Preset = 'road' | 'gravel' | 'quiet';
// What a saved route's `preset` may be - "custom" marks a route whose
// costing came from sliders rather than one of the three named bundles.
export type StoredPreset = Preset | 'custom';

/** One request's worth of costing overrides - mirrors
 * backend/app/schemas.py's BicycleCostingOptions, which is the server-side
 * allowlist these values are validated against. */
export interface BicycleCostingOptions {
	bicycle_type: 'Road' | 'Hybrid' | 'Cross' | 'Mountain';
	cycling_speed: number;
	use_roads: number;
	use_hills: number;
	avoid_bad_surfaces: number;
}

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

/** Cumulative elapsed time to a distance along the route, at the rider's
 * effective flat speed adjusted for gradient and surface. Computed fresh on
 * every read from the current settings - never stored - and unrelated to
 * `duration_s`, which is the routing engine's own estimate. */
export interface RideTimePoint {
	dist_m: number;
	time_s: number;
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

export interface SurfaceBreakdown {
	total_m: number;
	/** Keyed by Valhalla's own enum strings, including ones absent from its
	 * documented enum (e.g. "service_road", "parking_aisle"). */
	surface_m: Record<string, number>;
	road_class_m: Record<string, number>;
	use_m: Record<string, number>;
	/** Metres where cycle_lane is present and not "none". */
	cycle_lane_m: number;
}

export interface Climb {
	start_dist_m: number;
	end_dist_m: number;
	length_m: number;
	gain_m: number;
	avg_grade_pct: number;
	max_grade_pct: number;
	score: number;
	/** HC, "1", "2", "3" or "4" - HC steepest/longest. */
	category: string;
}

export interface RouteResponse {
	legs: RouteLeg[];
	distance_m: number;
	duration_s: number;
	ascent_m: number;
	descent_m: number;
	elevation: ElevationPoint[];
	/** Null for snapshots stored before Phase 7, and for any route whose
	 * edge_walk match failed - surface is decorative and never blocks a save. */
	surface?: SurfaceBreakdown | null;
	/** Ordered by start_dist_m. Empty for snapshots stored before climb
	 * detection shipped, and for routes with no detected climbs. */
	climbs: Climb[];
	/** Empty when there is no elevation to walk (e.g. no elevation tiles
	 * built). Computed on read for the viewer's rider settings. */
	ride_time: RideTimePoint[];
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
	/** False unless WEATHER_API_URL is set on the backend. Gates the weather
	 * panel entirely - nothing reaches outside the LAN unless configured. */
	weather_enabled: boolean;
}

const CONFIG_FALLBACK: AppConfig = {
	tile_url_cyclosm: null,
	search_enabled: false,
	search_index_version: null,
	weather_enabled: false
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

export interface WindSegment {
	lat: number;
	lon: number;
	dist_along_m: number;
	arrival_iso: string;
	wind_speed_ms: number;
	wind_direction_deg: number;
	/** Positive slows you down, negative helps - a tailwind reads as a
	 * negative headwind rather than a separate sign flag. */
	headwind_ms: number;
	crosswind_ms: number;
}

export interface WeatherAlongRoute {
	segments: WindSegment[];
	/** True when the start time fell outside the forecast provider's window
	 * (or the request otherwise degraded) - shown rather than presenting an
	 * empty result as "no wind". */
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
		version: string;
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
	preset: StoredPreset;
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
	preset: StoredPreset;
	/** Echoed back so reloading a route can restore the sliders and the
	 * Custom pill. Null when a named preset was used instead. */
	costing_options: BicycleCostingOptions | null;
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
	// Stored text, present only when it still describes the route's current
	// geometry. Absent whenever the assistant was not configured at share
	// time, or the route has been re-routed since.
	summary: string | null;
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
	preset: StoredPreset;
	/** Only applied by the backend when `preset` is also sent - see
	 * api/routes.py:update_route. */
	costing_options?: BicycleCostingOptions | null;
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

/** Routing modes the gateway offers. 'balanced' is its own default. */
export type ProviderSort = 'balanced' | 'price' | 'throughput' | 'latency';

export interface LLMSettings {
	base_url: string | null;
	model: string | null;
	provider_order: string | null;
	provider_sort: ProviderSort | null;
	max_prompt_price: number | null;
	/** Whether a key is stored. The key itself is never returned. */
	api_key_set: boolean;
	effective_base_url: string;
	effective_model: string;
	effective_providers: string[];
	enabled: boolean;
	/** True when the env vars alone would configure the assistant. */
	env_configured: boolean;
}

export interface LLMSettingsPatch {
	base_url?: string;
	model?: string;
	provider_order?: string;
	provider_sort?: string;
	max_prompt_price?: number | null;
	/** Write-only. Send an empty string to clear a stored key. */
	api_key?: string;
}

export interface LLMModelInfo {
	id: string;
	name: string;
	/** Dollars per million tokens, or null when the gateway does not say. */
	prompt_price: number | null;
	completion_price: number | null;
	context_length: number | null;
	supports_tools: boolean;
}

export interface LLMProviderInfo {
	name: string;
	prompt_price: number | null;
	completion_price: number | null;
	context_length: number | null;
}

export interface LLMProbeResult {
	ok: boolean;
	latency_s: number;
	model: string;
	provider: string | null;
	cost: number | null;
	tool_called: boolean;
	detail: string;
}

export const llmAdmin = {
	get: () => request<LLMSettings>('/api/admin/llm'),
	update: (patch: LLMSettingsPatch) =>
		request<LLMSettings>('/api/admin/llm', { method: 'PATCH', body: JSON.stringify(patch) }),
	models: (toolsOnly = true) =>
		request<LLMModelInfo[]>(`/api/admin/llm/models?tools_only=${toolsOnly}`),
	providers: (modelId: string) =>
		request<LLMProviderInfo[]>(`/api/admin/llm/models/${modelId}/providers`),
	test: () => request<LLMProbeResult>('/api/admin/llm/test', { method: 'POST' })
};

export interface CustomPreset {
	id: string;
	name: string;
	options: BicycleCostingOptions;
}

export const customPresets = {
	list: () => request<CustomPreset[]>('/api/custom-presets'),
	create: (name: string, options: BicycleCostingOptions) =>
		request<CustomPreset>('/api/custom-presets', {
			method: 'POST',
			body: JSON.stringify({ name, options })
		}),
	update: (id: string, patch: { name?: string; options?: BicycleCostingOptions }) =>
		request<CustomPreset>(`/api/custom-presets/${id}`, {
			method: 'PATCH',
			body: JSON.stringify(patch)
		}),
	remove: (id: string) => request<void>(`/api/custom-presets/${id}`, { method: 'DELETE' })
};

export interface UserSettings {
	weight_kg: number;
	flat_speed_kmh: number;
	ftp_watts: number | null;
	version: string;
}

export const settings = {
	get: () => request<UserSettings>('/api/settings'),
	update: (patch: Partial<UserSettings>) =>
		request<UserSettings>('/api/settings', { method: 'PATCH', body: JSON.stringify(patch) })
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

export interface RouteSurfaceResult {
	surface: SurfaceBreakdown | null;
	/** Empty when no elevation was sent - the caller then falls back to
	 * whatever ride_time it already has. */
	ride_time: RideTimePoint[];
}

/** Surface/road-class/use breakdown for a route, via /trace_attributes with
 * edge_walk over each leg separately (a via-waypoint route's concatenated
 * shape fails edge_walk at the joins). Optionally recomputes ride_time from
 * the rider's settings against the fresh surface. Takes a signal because the
 * route can change underneath a request that is still in flight; resolves to
 * a null surface when a leg does not lie exactly on the routing graph
 * (edge_walk fails by design) or the request otherwise fails - surface is
 * decorative, never blocking. */
export async function routeSurface(
	legs: [number, number][][],
	elevation: ElevationPoint[] | null,
	signal?: AbortSignal
): Promise<RouteSurfaceResult> {
	const response = await fetch('/api/route/surface', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ legs, elevation }),
		signal
	});
	if (!response.ok) return { surface: null, ride_time: [] };
	return response.json();
}

/** Wind sampled along a route line for a chosen start time. Only ever
 * called on an explicit user action (the "Show wind" button) - never from
 * an effect - because it is the one call in the app that reaches an
 * external service. */
export async function routeWeather(
	line: [number, number][],
	startTime: Date,
	durationS: number | null,
	rideTime: RideTimePoint[] = [],
	signal?: AbortSignal
): Promise<WeatherAlongRoute> {
	const response = await fetch('/api/route/weather', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({
			line,
			start_time: startTime.toISOString(),
			duration_s: durationS,
			// The gradient/surface-aware profile, when the route has one:
			// without it the backend times wind samples off the flat
			// duration and looks up the summit's wind for the wrong hour.
			ride_time: rideTime.length ? rideTime : null
		}),
		signal
	});
	if (!response.ok) {
		let detail = `Weather lookup failed (${response.status})`;
		try {
			const body = await response.json();
			if (typeof body.detail === 'string') detail = body.detail;
		} catch {
			// keep the generic message
		}
		throw new ApiError(response.status, detail);
	}
	return await response.json();
}

export async function planRoute(
	waypoints: Waypoint[],
	preset: Preset,
	costingOptions: BicycleCostingOptions | null = null,
	excludeLocations: Waypoint[] | null = null,
	signal?: AbortSignal
): Promise<RouteResponse> {
	const response = await fetch('/api/route', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({
			waypoints,
			preset,
			costing_options: costingOptions,
			exclude_locations: excludeLocations
		}),
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

/** One reachability ring - mirrors backend/app/schemas.py's
 * IsochroneContour, minus the km option: the planner only ever asks for a
 * single time-based contour today. */
export interface IsochroneContour {
	minutes: number;
	color: string;
}

/** Valhalla's own GeoJSON, passed through untouched - MapLibre reads each
 * feature's own `fill`/`color`/`fillOpacity` properties directly. */
export interface IsochroneResult {
	type: string;
	features: Feature[];
}

/** How far a rider can get from a point in `minutes`, as a GeoJSON polygon.
 * Only ever called on an explicit "Show isochrone" click - never from an
 * effect - matching routeWeather's convention for calls the rider has to
 * ask for. */
export async function routeIsochrone(
	origin: Waypoint,
	minutes: number,
	preset: Preset,
	costingOptions: BicycleCostingOptions | null = null,
	signal?: AbortSignal
): Promise<IsochroneResult> {
	return request<IsochroneResult>('/api/route/isochrone', {
		method: 'POST',
		body: JSON.stringify({
			origin,
			contours: [{ minutes, color: '268bd2' }],
			preset,
			costing_options: costingOptions
		}),
		signal
	});
}

/** One candidate loop: an ordinary out-and-back route through a single via
 * point, with its full RouteResponse snapshot already computed - picking
 * one needs no second round trip to plan it again. */
export interface LoopCandidate {
	waypoints: Waypoint[];
	snapshot: RouteResponse;
	distance_error_km: number;
	score: number;
}

export interface LoopCandidatesResponse {
	candidates: LoopCandidate[];
}

/** "N km loop from here" (services/loop.py): dozens of LAN Valhalla round
 * trips server-side, so this call is legitimately slow - no artificial
 * client-side timeout is applied, unlike planRoute and routeSurface.
 * The active preset rides along so candidates route (and score their
 * surface mix) the way the rider actually rides; a custom-costing bundle
 * overrides it entirely - see resolve_costing. */
export async function routeLoop(
	origin: Waypoint,
	targetKm: number,
	preset: Preset,
	costingOptions: BicycleCostingOptions | null = null,
	excludeLocations: Waypoint[] | null = null,
	signal?: AbortSignal
): Promise<LoopCandidatesResponse> {
	return request<LoopCandidatesResponse>('/api/route/loop', {
		method: 'POST',
		body: JSON.stringify({
			origin,
			target_km: targetKm,
			preset,
			costing_options: costingOptions,
			exclude_locations: excludeLocations
		}),
		signal
	});
}

export interface AlternatesResult {
	primary: RouteResponse;
	alternates: RouteResponse[];
}

/** Valhalla `alternates`, only ever valid for a single origin/destination
 * pair (its own documented limitation) - `waypoints` here must be exactly
 * 2 or the backend rejects it with a 422 before Valhalla is asked at all.
 * Only ever called on an explicit "Alternatives" click, matching
 * routeWeather/routeIsochrone/routeLoop's convention for calls the rider
 * has to ask for. */
export async function routeAlternates(
	waypoints: [Waypoint, Waypoint],
	preset: Preset,
	costingOptions: BicycleCostingOptions | null = null,
	excludeLocations: Waypoint[] | null = null,
	count = 2,
	signal?: AbortSignal
): Promise<AlternatesResult> {
	return request<AlternatesResult>('/api/route/alternates', {
		method: 'POST',
		body: JSON.stringify({
			waypoints,
			preset,
			costing_options: costingOptions,
			exclude_locations: excludeLocations,
			count
		}),
		signal
	});
}
