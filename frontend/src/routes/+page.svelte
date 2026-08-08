<script lang="ts">
	import { page } from '$app/state';
	import type { FeatureCollection } from 'geojson';
	import {
		fetchConfig,
		places,
		planRoute,
		routeAlternates,
		routeIsochrone,
		routeLoop,
		routeSurface,
		routeWeather,
		routes,
		wahoo,
		type AppConfig,
		type BicycleCostingOptions,
		type IsochroneResult,
		type LoopCandidate,
		type PlaceResult,
		type PoiResult,
		type Preset,
		type RouteResponse,
		type RouteSource,
		type StoredPreset,
		type WahooStatus,
		type Waypoint,
		type WindSegment
	} from '$lib/api';
	import { cumulativeDistances, pointAtDistance } from '$lib/geo';
	import { gradientSegments as computeGradientSegments } from '$lib/gradient';
	import { decodePolyline6 } from '$lib/polyline';
	import { history, type PlannerSnapshot } from '$lib/history.svelte';
	import { categoriesFor, DEFAULT_POI_GROUPS } from '$lib/pois';
	import AlternatesPanel from '$lib/components/AlternatesPanel.svelte';
	import ClimbsList from '$lib/components/ClimbsList.svelte';
	import ElevationProfile from '$lib/components/ElevationProfile.svelte';
	import LoopPanel from '$lib/components/LoopPanel.svelte';
	import PlaceSearch from '$lib/components/PlaceSearch.svelte';
	import PoiPanel from '$lib/components/PoiPanel.svelte';
	import PresetSelector from '$lib/components/PresetSelector.svelte';
	import PresetSlidersPopover from '$lib/components/PresetSlidersPopover.svelte';
	import SurfaceBar from '$lib/components/SurfaceBar.svelte';
	import WaypointList from '$lib/components/WaypointList.svelte';
	import WeatherPanel from '$lib/components/WeatherPanel.svelte';
	import MapView from '$lib/map/MapView.svelte';
	import { unsaved } from '$lib/unsaved.svelte';
	import { onDestroy } from 'svelte';

	let waypoints: Waypoint[] = $state([]);
	let preset: Preset = $state('road');
	// Set when the "Custom" pill is active - overrides `preset` entirely for
	// routing, and is what makes a saved route's stored preset "custom".
	let customCostingOptions: BicycleCostingOptions | null = $state(null);
	let customPopoverOpen = $state(false);
	let route: RouteResponse | null = $state(null);
	let loading = $state(false);
	let error: string | null = $state(null);
	let hoverPoint: [number, number] | null = $state(null);
	let config: AppConfig | null = $state(null);

	let fitTrigger = $state(0);
	let flyTo: Waypoint | null = $state(null);
	let flyTrigger = $state(0);
	let mapCentre: Waypoint | null = $state(null);
	let savedId: string | null = $state(null);
	let savedName: string | null = $state(null);
	let dirty = $state(false);
	// Bumped at every site that sets `dirty = true`. A save that started
	// before a later edit landed must not clear `dirty` for that later edit -
	// it captures this before its own await chain and only clears dirty if
	// nothing has bumped it since.
	let editGeneration = 0;
	let saveDialogOpen = $state(false);
	let saveNameInput = $state('');
	let saving = $state(false);
	// Points of interest. `poiGroups` survives across routes on purpose: the
	// rider who wants water shown wants it shown on the next ride too.
	let poiGroups: string[] = $state([...DEFAULT_POI_GROUPS]);
	let pois: PoiResult[] = $state([]);
	let poisTruncated = $state(false);
	let poisLoading = $state(false);
	let hoveredPoiId: number | null = $state(null);
	let poiController: AbortController | null = null;
	// Shared by ClimbsList (row hover), ElevationProfile (highlight band) and
	// MapView (highlight casing) - one index rather than three copies of it.
	let hoveredClimbIndex: number | null = $state(null);
	// Shared by WaypointList (row hover) and MapView (marker highlight).
	let hoveredWaypointIndex: number | null = $state(null);

	// Wind along the route. Unlike POIs and surface, this never fetches from
	// an effect - it is the one call in the app that reaches an external
	// service, so it only ever runs from the "Show wind" button.
	let weatherStartTime = $state(nextFullHourLocal());
	let windSegments: WindSegment[] = $state([]);
	let windTruncated = $state(false);
	let windLoading = $state(false);
	let windError: string | null = $state(null);
	let windController: AbortController | null = null;

	// Isochrone ("how far can I get in N minutes"), opened from the map's
	// context menu. Origin-anchored rather than route-anchored: it survives
	// a reroute or waypoint edit and is cleared only by clear() or the
	// "Hide isochrone" button, matching wind's "only on explicit user
	// action" convention for the one other call that hits an external-ish
	// service (here, self-hosted Valhalla, but still not free to spam).
	let isochroneData: FeatureCollection | null = $state(null);
	let isochroneOrigin: Waypoint | null = $state(null);
	let isochronePromptWp: Waypoint | null = $state(null);
	let isochronePromptOpen = $state(false);
	let isochroneMinutes = $state(60);
	let isochroneLoading = $state(false);
	let isochroneError: string | null = $state(null);
	let isochroneController: AbortController | null = null;
	// "Loop from here" (services/loop.py). loopOrigin non-null is what opens
	// the card - set by the map's context menu, cleared by using a loop,
	// dismissing the card, or clear(). loopCandidates stays null until the
	// first search settles, distinguishing "hasn't searched yet" from "found
	// nothing" ([]).
	let loopOrigin: Waypoint | null = $state(null);
	let loopTargetKm = $state(60);
	let loopCandidates: LoopCandidate[] | null = $state(null);
	let loopLoading = $state(false);
	let loopError: string | null = $state(null);
	let hoveredLoopIndex: number | null = $state(null);

	// Route alternates ("Alternatives" button). Valhalla's `alternates` only
	// ever applies to a single origin/destination pair, so this is an
	// explicit button rather than something that runs alongside every
	// reroute - see AlternatesQuery's docstring. alternatesOpen gates the
	// panel; alternates itself stays null until the first search settles,
	// distinguishing "hasn't searched yet" from "found nothing" ([]).
	let alternatesOpen = $state(false);
	let alternates: RouteResponse[] | null = $state(null);
	let alternatesLoading = $state(false);
	let alternatesError: string | null = $state(null);
	let hoveredAlternateIndex: number | null = $state(null);
	// The waypoints `alternates` was fetched against, as a plain (non-$state)
	// snapshot - compared by value in the invalidation effect below, so
	// populating `alternates` itself never trips that effect's own guard.
	let alternatesFetchedFor: Waypoint[] | null = null;
	// Points to route around ("not that road"), from the map's context menu.
	// Session-only planner memory - never persisted with a saved route, since
	// the saved geometry already reflects whatever avoids shaped it.
	let avoids: Waypoint[] = $state([]);

	let wahooStatus: WahooStatus | null = $state(null);
	let wahooPush: 'idle' | 'working' | 'synced' | 'error' = $state('idle');
	let wahooPushError: string | null = $state(null);

	let abortController: AbortController | null = null;

	fetchConfig().then((c) => (config = c));
	wahoo.status().then((s) => (wahooStatus = s));

	// Bumped whenever the push in flight stops describing what is on screen.
	// A poll that outlives its generation must not write status: it would
	// resurrect "Sent to Wahoo" for a route that has since been edited.
	let wahooGeneration = 0;

	async function sendToWahoo() {
		if (!savedId) return;
		const generation = wahooGeneration;
		wahooPush = 'working';
		wahooPushError = null;
		try {
			await wahoo.push(savedId);
			await pollWahoo(savedId, generation);
		} catch (err) {
			if (generation !== wahooGeneration) return;
			wahooPush = 'error';
			wahooPushError = err instanceof Error ? err.message : 'Push failed';
		}
	}

	async function pollWahoo(id: string, generation: number) {
		for (let i = 0; i < 40; i++) {
			await new Promise((resolve) => setTimeout(resolve, 3000));
			if (savedId !== id || generation !== wahooGeneration) return;
			const saved = await routes.get(id);
			if (generation !== wahooGeneration) return;
			if (saved.wahoo.status === 'synced') {
				wahooPush = 'synced';
				return;
			}
			if (saved.wahoo.status === 'error') {
				wahooPush = 'error';
				wahooPushError = saved.wahoo.error;
				return;
			}
		}
		if (generation !== wahooGeneration) return;
		wahooPush = 'error';
		wahooPushError = 'Push is taking unusually long - check the library later';
	}

	// A Wahoo push describes one particular saved route. Clearing the map,
	// opening a different route, or editing this one all make "Sent to Wahoo"
	// a false claim about what is on the head unit - and a push still in
	// flight when the route changes leaves the button disabled for good,
	// because pollWahoo bails out silently once savedId has moved on.
	$effect(() => {
		void savedId;
		void dirty;
		wahooGeneration += 1;
		wahooPush = 'idle';
		wahooPushError = null;
	});

	// Published so the window-wide file drop in the layout can ask before
	// navigating away from unsaved work.
	$effect(() => {
		unsaved.dirty = dirty && waypoints.length > 0;
	});
	onDestroy(() => {
		unsaved.dirty = false;
	});

	// An imported route's line is the track that was uploaded; its waypoints
	// are only the endpoints. Re-routing between them would throw the track
	// away, so ask before the first edit rather than losing it silently.
	let source: RouteSource = $state('planned');

	function mayEdit(): boolean {
		if (source !== 'imported') return true;
		const ok = confirm(
			'This route was imported from a file.\n\n' +
				'Editing re-routes it between its start and end points, which discards the ' +
				'imported track and its turn cues. Continue?'
		);
		if (ok) source = 'planned';
		return ok;
	}

	// The inputs a mutator changes, captured before the mutation lands -
	// history.push deep-copies this itself, so the caller does not need to.
	function currentSnapshot(): PlannerSnapshot {
		return {
			waypoints,
			preset,
			costingOptions: customCostingOptions,
			source,
			avoidLocations: avoids,
			routeOverride: null
		};
	}

	// Restores a history entry. Time travel bypasses mayEdit() - it is not a
	// fresh editorial decision, and the imported-source guard still holds
	// because `source` itself travels in the snapshot.
	function applySnapshot(snap: PlannerSnapshot) {
		waypoints = snap.waypoints.map((wp) => ({ ...wp }));
		preset = snap.preset;
		customCostingOptions = snap.costingOptions ? { ...snap.costingOptions } : null;
		source = snap.source;
		avoids = snap.avoidLocations.map((wp) => ({ ...wp }));
		if (snap.routeOverride) {
			route = snap.routeOverride;
			dirty = true;
			editGeneration += 1;
		} else {
			reroute();
		}
	}

	// Open a saved route when arriving via /?route=<id>.
	const routeParam = page.url.searchParams.get('route');
	if (routeParam) {
		routes
			.get(routeParam)
			.then((saved) => {
				waypoints = saved.waypoints;
				preset = saved.preset === 'custom' ? 'road' : saved.preset;
				customCostingOptions = saved.costing_options;
				source = saved.source;
				// Session-only: a loaded route's geometry already reflects
				// whatever avoids shaped it, and none carried over from before.
				avoids = [];
				route = saved;
				savedId = saved.id;
				savedName = saved.name;
				dirty = false;
				fitTrigger += 1;
				// A freshly loaded route must not let undo reach back into
				// whatever was on screen before it.
				history.clear();
			})
			.catch(() => {
				error = 'Could not load that route.';
			});
	}

	const decodedLegs = $derived.by(() =>
		route ? route.legs.map((leg) => decodePolyline6(leg.geometry)) : []
	);
	const routeLine = $derived.by(() => {
		const merged: [number, number][] = [];
		for (const leg of decodedLegs) {
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
	});
	const legStartIndices = $derived.by(() => {
		const starts: number[] = [];
		let offset = 0;
		for (const leg of decodedLegs) {
			starts.push(offset);
			offset += Math.max(leg.length - 1, 0);
		}
		return starts;
	});
	const routeDists = $derived(cumulativeDistances(routeLine));
	const gradientSegments = $derived.by(() =>
		computeGradientSegments(route?.elevation ?? [], routeLine, routeDists)
	);
	const hoveredClimb = $derived.by(() => {
		const climbs = route?.climbs ?? [];
		if (hoveredClimbIndex === null) return null;
		const climb = climbs[hoveredClimbIndex];
		return climb ? { start_dist_m: climb.start_dist_m, end_dist_m: climb.end_dist_m } : null;
	});

	/** Merge one candidate's legs into a single line, the same join rule
	 * `routeLine` above uses - duplicated rather than shared, since a loop
	 * candidate's snapshot is a plain RouteResponse but never assigned to
	 * `route` itself (that only happens once one is picked, in useLoop). */
	function mergeLegLines(legs: [number, number][][]): [number, number][] {
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

	const loopPreviews = $derived.by(() => {
		if (!loopCandidates) return null;
		return loopCandidates.map((candidate, index) => ({
			index,
			coords: mergeLegLines(candidate.snapshot.legs.map((leg) => decodePolyline6(leg.geometry)))
		}));
	});

	const alternatePreviews = $derived.by(() => {
		if (!alternates) return null;
		return alternates.map((alt, index) => ({
			index,
			coords: mergeLegLines(alt.legs.map((leg) => decodePolyline6(leg.geometry)))
		}));
	});

	// Far enough to catch a cafe just off the road, near enough that
	// "on this ride" still means something.
	const POI_RADIUS_M = 250;

	// Refetch whenever the route or the chosen categories change. Requests
	// supersede one another: dragging a waypoint re-plans repeatedly, and a
	// stale answer landing last would describe a route that no longer exists.
	$effect(() => {
		const line = routeLine;
		const groups = poiGroups;
		const enabled = config?.search_enabled ?? false;
		poiController?.abort();
		if (!enabled || line.length < 2 || groups.length === 0) {
			pois = [];
			poisTruncated = false;
			poisLoading = false;
			return;
		}
		const controller = new AbortController();
		poiController = controller;
		poisLoading = true;
		places
			.poisAlongRoute(line, categoriesFor(groups), POI_RADIUS_M, controller.signal)
			.then((result) => {
				if (controller.signal.aborted) return;
				pois = result.pois;
				poisTruncated = result.truncated;
			})
			.catch(() => {
				if (controller.signal.aborted) return;
				pois = [];
				poisTruncated = false;
			})
			.finally(() => {
				if (!controller.signal.aborted) poisLoading = false;
			});
	});

	let surfaceController: AbortController | null = null;
	// The in-flight surface fetch, awaited by the save paths: a reroute
	// replaces `route` with a response whose surface is always null, so
	// saving before this settles would silently erase a stored breakdown.
	let surfacePending: Promise<void> | null = null;

	// Refetch the surface breakdown whenever the route or preset changes.
	// Sent per-leg (a via-waypoint route's concatenated shape fails
	// edge_walk at the joins) and with the route's own elevation, so the
	// response also carries a ride_time recomputed against the fresh
	// surface. Written straight onto `route.surface`/`route.ride_time`
	// (rather than replacing `route` itself) so the existing save path
	// persists it with zero special-casing, without reassigning `route`
	// wholesale - which would retrigger decodedLegs' own derivation and
	// loop this effect back on itself. `route.elevation` only changes when
	// `route` itself is replaced (by reroute or by loading a saved route),
	// which already retriggers this effect via `decodedLegs` (derived from
	// `route.legs`), so reading it here does not need to be tracked
	// separately.
	$effect(() => {
		const legs = decodedLegs;
		const currentPreset = preset;
		const currentCustom = customCostingOptions;
		const elevation = route?.elevation ?? null;
		surfaceController?.abort();
		if (legs.length === 0) {
			if (route) route.surface = null;
			surfacePending = null;
			return;
		}
		const controller = new AbortController();
		surfaceController = controller;
		surfacePending = routeSurface(legs, currentPreset, elevation, currentCustom, controller.signal)
			.then((result) => {
				if (controller.signal.aborted || !route) return;
				route.surface = result.surface;
				// The live estimate starts paved-equivalent (plan_route computes
				// it before the surface breakdown exists); this is what refines
				// it once the real surface is known.
				if (result.ride_time.length) route.ride_time = result.ride_time;
			})
			.catch(() => {
				if (controller.signal.aborted || !route) return;
				route.surface = null;
			});
	});

	function togglePoiGroup(key: string) {
		poiGroups = poiGroups.includes(key) ? poiGroups.filter((k) => k !== key) : [...poiGroups, key];
	}

	// A stale wind result describing a route the rider has since dragged
	// somewhere else is worse than no result - clear it, but never refetch:
	// that stays behind the "Show wind" button.
	$effect(() => {
		void routeLine;
		windController?.abort();
		windSegments = [];
		windTruncated = false;
		windError = null;
		// The abort above never resolves showWind's own finally (an aborted
		// fetch's promise settles asynchronously, after this effect has
		// already run), so it must reset the spinner itself here too.
		windLoading = false;
	});

	function nextFullHourLocal(): string {
		const now = new Date();
		const next = new Date(now.getFullYear(), now.getMonth(), now.getDate(), now.getHours() + 1);
		const pad = (n: number) => String(n).padStart(2, '0');
		return `${next.getFullYear()}-${pad(next.getMonth() + 1)}-${pad(next.getDate())}T${pad(next.getHours())}:${pad(next.getMinutes())}`;
	}

	async function showWind() {
		const line = routeLine;
		if (line.length < 2 || !weatherStartTime) return;
		windController?.abort();
		const controller = new AbortController();
		windController = controller;
		windLoading = true;
		windError = null;
		try {
			const startTime = new Date(weatherStartTime);
			const result = await routeWeather(
				line,
				startTime,
				route?.duration_s ?? null,
				route?.ride_time ?? [],
				controller.signal
			);
			if (controller.signal.aborted) return;
			windSegments = result.segments;
			windTruncated = result.truncated;
		} catch (err) {
			if (controller.signal.aborted) return;
			windSegments = [];
			windTruncated = false;
			windError = err instanceof Error ? err.message : 'Weather lookup failed';
		} finally {
			// Only clear the spinner for the request that is still current -
			// a newer "Show wind" click (or the route-change effect) may
			// already have moved windController on, and its own spinner must
			// not be clobbered by this older request settling late.
			if (windController === controller) windLoading = false;
		}
	}

	// Opens the small inline prompt from the map's context menu; the fetch
	// itself waits for "Show isochrone" so a stray right-click never spends
	// a Valhalla round trip.
	function openIsochronePrompt(wp: Waypoint) {
		isochronePromptWp = wp;
		isochronePromptOpen = true;
		isochroneError = null;
	}

	async function showIsochrone() {
		const wp = isochronePromptWp;
		if (!wp) return;
		isochroneController?.abort();
		const controller = new AbortController();
		isochroneController = controller;
		isochroneLoading = true;
		isochroneError = null;
		try {
			const result: IsochroneResult = await routeIsochrone(
				wp,
				isochroneMinutes,
				preset,
				customCostingOptions,
				controller.signal
			);
			if (controller.signal.aborted) return;
			// Valhalla's `type` is always "FeatureCollection" in practice;
			// IsochroneResponse types it as a plain string only because
			// extra="allow" lets unrelated future fields through untouched.
			isochroneData = result as FeatureCollection;
			isochroneOrigin = wp;
			isochronePromptOpen = false;
		} catch (err) {
			if (controller.signal.aborted) return;
			isochroneError = err instanceof Error ? err.message : 'Isochrone lookup failed';
		} finally {
			if (isochroneController === controller) isochroneLoading = false;
		}
	}

	function hideIsochrone() {
		isochroneController?.abort();
		isochroneData = null;
		isochroneOrigin = null;
	}

	async function reroute() {
		abortController?.abort();
		if (waypoints.length < 2) {
			route = null;
			error = null;
			return;
		}
		abortController = new AbortController();
		loading = true;
		error = null;
		try {
			route = await planRoute(
				waypoints,
				preset,
				customCostingOptions,
				avoids.length ? avoids : null,
				abortController.signal
			);
			loading = false;
			dirty = true;
			editGeneration += 1;
		} catch (err) {
			if (err instanceof DOMException && err.name === 'AbortError') return;
			loading = false;
			error = err instanceof Error ? err.message : 'Routing failed';
		}
	}

	function addWaypoint(wp: Waypoint) {
		if (!mayEdit()) return;
		history.push(currentSnapshot());
		waypoints.push(wp);
		reroute();
	}

	function moveWaypoint(index: number, wp: Waypoint) {
		if (!mayEdit()) return;
		history.push(currentSnapshot());
		waypoints[index] = wp;
		reroute();
	}

	function insertVia(position: number, wp: Waypoint) {
		if (!mayEdit()) return;
		history.push(currentSnapshot());
		waypoints.splice(position, 0, wp);
		reroute();
	}

	function removeWaypoint(index: number) {
		if (!mayEdit()) return;
		history.push(currentSnapshot());
		waypoints.splice(index, 1);
		reroute();
	}

	// Waypoint list panel: up/down buttons and native drag-and-drop both
	// funnel through this. Splice-out-splice-in rather than a swap, so
	// dragging a row several places in one go (not just adjacent, as the
	// buttons always do) still lands it exactly where it was dropped.
	function reorderWaypoint(from: number, to: number) {
		if (!mayEdit()) return;
		if (to < 0 || to >= waypoints.length || from === to) return;
		history.push(currentSnapshot());
		const [wp] = waypoints.splice(from, 1);
		waypoints.splice(to, 0, wp);
		reroute();
	}

	// Gated by mayEdit() like every other mutator: an avoid only means
	// anything alongside waypoints to route between, and adding one to an
	// imported route's planner state is exactly the "re-route this" edit
	// mayEdit() exists to confirm.
	function addAvoid(wp: Waypoint) {
		if (!mayEdit()) return;
		history.push(currentSnapshot());
		avoids.push(wp);
		reroute();
	}

	function removeAvoid(index: number) {
		if (!mayEdit()) return;
		history.push(currentSnapshot());
		avoids.splice(index, 1);
		reroute();
	}

	function setStart(wp: Waypoint) {
		if (!mayEdit()) return;
		history.push(currentSnapshot());
		if (waypoints.length === 0) waypoints.push(wp);
		else waypoints[0] = wp;
		reroute();
	}

	function setEnd(wp: Waypoint) {
		if (!mayEdit()) return;
		history.push(currentSnapshot());
		if (waypoints.length < 2) waypoints.push(wp);
		else waypoints[waypoints.length - 1] = wp;
		reroute();
	}

	function pickPlace(place: PlaceResult, action: 'from' | 'add' | 'to') {
		const wp: Waypoint = { lat: place.lat, lon: place.lon };
		// Always show where it is, even when the edit is refused.
		flyTo = wp;
		flyTrigger += 1;
		if (action === 'from') setStart(wp);
		else if (action === 'to') setEnd(wp);
		else addWaypoint(wp);
	}

	function undo() {
		const snap = history.undo(currentSnapshot());
		if (snap) applySnapshot(snap);
	}

	function redo() {
		const snap = history.redo(currentSnapshot());
		if (snap) applySnapshot(snap);
	}

	// Cmd/Ctrl+Z undoes, Cmd/Ctrl+Shift+Z redoes - ignored while typing
	// anywhere text can be entered (save dialog name, isochrone minutes,
	// loop target, search box, sliders popover), where Z is just a letter.
	function handleKeydown(event: KeyboardEvent) {
		if (event.key.toLowerCase() !== 'z' || !(event.metaKey || event.ctrlKey)) return;
		const target = event.target as HTMLElement | null;
		if (
			target &&
			(target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName))
		) {
			return;
		}
		event.preventDefault();
		if (event.shiftKey) redo();
		else undo();
	}

	function clear() {
		// Pushed rather than dropped: an accidental Clear is exactly what
		// undo is for, so history is left alone here - only the ?route= load
		// path resets it.
		history.push(currentSnapshot());
		source = 'planned';
		waypoints = [];
		avoids = [];
		route = null;
		error = null;
		savedId = null;
		savedName = null;
		dirty = false;
		hideIsochrone();
		dismissLoop();
		dismissAlternates();
	}

	function onLoop(wp: Waypoint) {
		loopOrigin = wp;
		loopCandidates = null;
		loopError = null;
		hoveredLoopIndex = null;
	}

	function dismissLoop() {
		loopOrigin = null;
		loopCandidates = null;
		loopError = null;
		loopLoading = false;
		hoveredLoopIndex = null;
	}

	async function findLoops() {
		if (!loopOrigin) return;
		loopLoading = true;
		loopError = null;
		loopCandidates = null;
		try {
			loopCandidates = (await routeLoop(loopOrigin, loopTargetKm, preset, customCostingOptions))
				.candidates;
		} catch (err) {
			loopError = err instanceof Error ? err.message : 'Loop search failed';
		} finally {
			loopLoading = false;
		}
	}

	function sameWaypoints(a: Waypoint[], b: Waypoint[]): boolean {
		return a.length === b.length && a.every((wp, i) => wp.lat === b[i].lat && wp.lon === b[i].lon);
	}

	// Any waypoint edit invalidates whatever alternates were fetched for the
	// previous pair - reading `waypoints` is enough since every mutator
	// reassigns or mutates this $state array. Guarded by comparing against
	// alternatesFetchedFor (a plain snapshot, not itself reactive) so this
	// does not fire the instant a search populates `alternates`.
	$effect(() => {
		const current = waypoints;
		if (alternatesFetchedFor && !sameWaypoints(current, alternatesFetchedFor)) {
			dismissAlternates();
		}
	});

	function dismissAlternates() {
		alternatesOpen = false;
		alternates = null;
		alternatesError = null;
		alternatesLoading = false;
		hoveredAlternateIndex = null;
		alternatesFetchedFor = null;
	}

	async function findAlternates() {
		if (waypoints.length !== 2 || alternatesLoading) return;
		alternatesOpen = true;
		alternatesLoading = true;
		alternatesError = null;
		alternates = null;
		try {
			const result = await routeAlternates(
				[waypoints[0], waypoints[1]],
				preset,
				customCostingOptions
			);
			alternates = result.alternates;
			alternatesFetchedFor = waypoints.map((wp) => ({ ...wp }));
		} catch (err) {
			alternatesError = err instanceof Error ? err.message : 'Alternate route search failed';
		} finally {
			alternatesLoading = false;
		}
	}

	function useAlternate(index: number) {
		if (!mayEdit()) return;
		const picked = alternates?.[index];
		if (!picked || !route) return;
		// The first user of history's routeOverride escape hatch
		// (history.svelte.ts): adopting an alternate replaces the route
		// OUTPUT with no input change at all - the waypoints, preset and
		// costing options that produced the route already on screen are
		// exactly what produced this one too - so undo has to restore that
		// prior response verbatim rather than replay reroute(), which would
		// just fetch the current primary again.
		history.push({ ...currentSnapshot(), routeOverride: route });
		route = picked;
		dirty = true;
		editGeneration += 1;
		dismissAlternates();
	}

	function useLoop(candidate: LoopCandidate) {
		if (!mayEdit()) return;
		// An ordinary input change as far as undo is concerned: the pre-loop
		// waypoints/preset are what should come back.
		history.push(currentSnapshot());
		// Valhalla already ran for this candidate - reroute() would replan it
		// from scratch for no reason, so the snapshot goes straight onto
		// `route` instead, with the same dirty/editGeneration bump reroute()
		// itself does for every other edit.
		waypoints = candidate.waypoints;
		route = candidate.snapshot;
		dirty = true;
		editGeneration += 1;
		dismissLoop();
		fitTrigger += 1;
	}

	function defaultName(): string {
		return `Ride ${new Date().toLocaleDateString(undefined, { day: 'numeric', month: 'short' })}`;
	}

	async function placeNameAt(wp: Waypoint): Promise<string | null> {
		return (await places.reverse(wp.lat, wp.lon))?.name ?? null;
	}

	/** "Tring to Ivinghoe Beacon" rather than "Ride 7 Aug".
	 *
	 * The dialog opens straight away on the date-based fallback and this
	 * replaces it once the lookups land, but only while that fallback is
	 * still untouched - a name the rider has started typing is theirs.
	 */
	async function suggestName(fallback: string) {
		if (!config?.search_enabled) return;
		const [from, to] = await Promise.all([
			places.reverse(waypoints[0].lat, waypoints[0].lon),
			places.reverse(waypoints[waypoints.length - 1].lat, waypoints[waypoints.length - 1].lon)
		]);
		if (!from || !to) return;
		const suggestion = from.id === to.id ? `${from.name} loop` : `${from.name} to ${to.name}`;
		if (saveDialogOpen && saveNameInput === fallback) saveNameInput = suggestion;
	}

	// Waits out the surface fetch in flight when called, and any newer one
	// that starts while waiting - guards against drag, save, drag again
	// before the first fetch settles, where a bare `await surfacePending`
	// would resolve against a promise a later reroute has already replaced
	// and let save proceed with a stale (or null) surface.
	async function awaitSurfaceSettled() {
		while (surfacePending) {
			const pending = surfacePending;
			await pending;
			if (pending === surfacePending) break; // nothing newer started while we waited
		}
	}

	// "custom" is what marks a saved route as costed from the sliders rather
	// than one of the three named bundles - see api/routes.py's StoredPreset.
	const storedPreset = $derived<StoredPreset>(customCostingOptions ? 'custom' : preset);

	async function save() {
		if (!route || waypoints.length < 2) return;
		if (savedId === null) {
			const fallback = savedName ?? defaultName();
			saveNameInput = fallback;
			saveDialogOpen = true;
			void suggestName(fallback);
			return;
		}
		saving = true;
		try {
			await awaitSurfaceSettled();
			// Captured AFTER the settle wait, in the same synchronous block
			// as the snapshot read: the loop chases every fetch a mid-wait
			// reroute starts, so an edit landing during the wait is folded
			// into the snapshot and genuinely saved - counting it as unsaved
			// left dirty stuck true, blocking export and Wahoo push after a
			// save that persisted exactly what was on screen. Only an edit
			// landing during the HTTP round trip below is still unsaved.
			const gen = editGeneration;
			const snapshot = route;
			if (!snapshot) return;
			await routes.update(savedId, {
				waypoints,
				preset: storedPreset,
				costing_options: customCostingOptions,
				snapshot
			});
			if (gen === editGeneration) dirty = false;
		} catch (err) {
			error = err instanceof Error ? err.message : 'Save failed';
		} finally {
			saving = false;
		}
	}

	// Exports come from the stored route, so unsaved edits would silently
	// download the previous version.
	const canExport = $derived(savedId !== null && !dirty);
	const exportHint = $derived(
		dirty ? 'Save your changes first - exports come from the saved route' : ''
	);

	function download(format: 'gpx' | 'fit') {
		if (!savedId) return;
		window.location.href = format === 'gpx' ? routes.gpxUrl(savedId) : routes.fitUrl(savedId);
	}

	async function confirmSave(event: SubmitEvent) {
		event.preventDefault();
		if (!route || !saveNameInput.trim()) return;
		saving = true;
		try {
			await awaitSurfaceSettled();
			// See save(): captured after the settle wait, beside the
			// snapshot read - a mid-wait edit is in the snapshot and saved.
			const gen = editGeneration;
			const snapshot = route;
			if (!snapshot) return;
			const saved = await routes.create({
				name: saveNameInput.trim(),
				waypoints,
				preset: storedPreset,
				costing_options: customCostingOptions,
				snapshot
			});
			savedId = saved.id;
			savedName = saved.name;
			if (gen === editGeneration) dirty = false;
			saveDialogOpen = false;
		} catch (err) {
			error = err instanceof Error ? err.message : 'Save failed';
		} finally {
			saving = false;
		}
	}

	function changePreset(next: Preset) {
		history.push(currentSnapshot());
		preset = next;
		customCostingOptions = null;
		reroute();
	}

	function changeCustomCosting(next: BicycleCostingOptions) {
		history.push(currentSnapshot());
		customCostingOptions = next;
		reroute();
	}

	function handleElevationHover(distM: number | null) {
		hoverPoint = distM === null ? null : pointAtDistance(routeLine, routeDists, distM);
	}

	function formatDistance(m: number): string {
		return `${(m / 1000).toFixed(1)} km`;
	}

	function formatDuration(s: number): string {
		const h = Math.floor(s / 3600);
		const min = Math.round((s % 3600) / 60);
		return h ? `${h} h ${min.toString().padStart(2, '0')} min` : `${min} min`;
	}
</script>

<svelte:window onkeydown={handleKeydown} />

<div class="app">
	<div class="map-area">
		{#if config}
			<MapView
				{waypoints}
				{routeLine}
				{legStartIndices}
				{gradientSegments}
				{hoverPoint}
				cyclosmTileUrl={config.tile_url_cyclosm}
				{fitTrigger}
				{flyTo}
				{flyTrigger}
				onMapMove={(centre) => (mapCentre = centre)}
				resolvePlaceName={config.search_enabled ? placeNameAt : undefined}
				cycleNetworkAvailable={config.search_enabled}
				cycleNetworkVersion={config.search_index_version}
				{pois}
				{hoveredPoiId}
				onHoverPoi={(id) => (hoveredPoiId = id)}
				climbs={route?.climbs ?? []}
				{hoveredClimbIndex}
				{hoveredWaypointIndex}
				isochrone={isochroneData}
				{isochroneOrigin}
				onIsochrone={openIsochronePrompt}
				{onLoop}
				{loopPreviews}
				{hoveredLoopIndex}
				alternateLines={alternatePreviews}
				{hoveredAlternateIndex}
				onAlternateClick={useAlternate}
				{avoids}
				onAvoid={addAvoid}
				onAddWaypoint={addWaypoint}
				onMoveWaypoint={moveWaypoint}
				onInsertVia={insertVia}
				onRemoveWaypoint={removeWaypoint}
				onSetStart={setStart}
				onSetEnd={setEnd}
				onClear={clear}
			/>
		{/if}
		{#if config?.search_enabled}
			<div class="search-bar">
				<PlaceSearch near={mapCentre} onPick={pickPlace} />
			</div>
		{/if}
		{#if loopOrigin}
			<LoopPanel
				targetKm={loopTargetKm}
				onTargetKmChange={(value) => (loopTargetKm = value)}
				loading={loopLoading}
				error={loopError}
				candidates={loopCandidates}
				hoveredIndex={hoveredLoopIndex}
				onHover={(i) => (hoveredLoopIndex = i)}
				onFind={findLoops}
				onUse={useLoop}
				onDismiss={dismissLoop}
			/>
		{/if}
		{#if alternatesOpen && route}
			<AlternatesPanel
				current={route}
				loading={alternatesLoading}
				error={alternatesError}
				{alternates}
				hoveredIndex={hoveredAlternateIndex}
				onHover={(i) => (hoveredAlternateIndex = i)}
				onUse={useAlternate}
				onDismiss={dismissAlternates}
			/>
		{/if}
		<div class="toolbar">
			<div class="preset-anchor">
				<PresetSelector
					{preset}
					onChange={changePreset}
					customActive={customCostingOptions !== null}
					onCustomize={() => (customPopoverOpen = true)}
				/>
				{#if customPopoverOpen}
					<PresetSlidersPopover
						{preset}
						current={customCostingOptions}
						onApply={changeCustomCosting}
						onClose={() => (customPopoverOpen = false)}
					/>
				{/if}
			</div>
			<button type="button" onclick={undo} disabled={!history.canUndo}>Undo</button>
			<button type="button" onclick={redo} disabled={!history.canRedo}>Redo</button>
			<button
				type="button"
				onclick={findAlternates}
				disabled={waypoints.length !== 2 || loading}
				title={waypoints.length !== 2
					? "Alternate routes are only available for routes with a single start and finish - Valhalla's own limitation"
					: undefined}
			>
				{alternatesLoading ? 'Alternatives…' : 'Alternatives'}
			</button>
			<button type="button" onclick={clear} disabled={waypoints.length === 0}>Clear</button>
			<button
				type="button"
				class="save"
				onclick={save}
				disabled={!route || saving || (savedId !== null && !dirty)}
			>
				{savedId === null ? 'Save' : dirty ? 'Save changes' : 'Saved'}
			</button>
			{#if savedId}
				<button
					type="button"
					class="export"
					onclick={() => download('gpx')}
					disabled={!canExport}
					title={exportHint || 'Download this route as GPX'}>GPX</button
				>
				<button
					type="button"
					class="export"
					onclick={() => download('fit')}
					disabled={!canExport}
					title={exportHint || 'Download this route as a FIT course'}>FIT</button
				>
			{/if}
			{#if savedName}
				<span class="route-name">{savedName}{dirty ? ' *' : ''}</span>
			{/if}
			{#if wahooStatus?.connected && savedId}
				<button
					type="button"
					class="wahoo"
					onclick={sendToWahoo}
					disabled={wahooPush === 'working'}
					title={wahooPush === 'error' ? (wahooPushError ?? '') : 'Push to your Wahoo account'}
				>
					{wahooPush === 'working'
						? 'Sending…'
						: wahooPush === 'synced'
							? 'Sent to Wahoo ✓'
							: wahooPush === 'error'
								? 'Wahoo failed - retry'
								: 'Send to Wahoo'}
				</button>
			{/if}
			{#if isochroneData}
				<button type="button" onclick={hideIsochrone}>Hide isochrone</button>
			{/if}
		</div>
		{#if avoids.length > 0}
			<div class="avoids" role="group" aria-label="Avoided roads">
				{#each avoids as avoid, i (i)}
					<span class="avoid-chip" title={`Near ${avoid.lat.toFixed(4)}, ${avoid.lon.toFixed(4)}`}>
						Avoid {i + 1}
						<button
							type="button"
							class="avoid-remove"
							onclick={() => removeAvoid(i)}
							aria-label={`Remove avoid ${i + 1}`}
						>
							×
						</button>
					</span>
				{/each}
			</div>
		{/if}
		{#if isochronePromptOpen}
			<div class="isochrone-prompt">
				<label>
					Minutes
					<input type="number" min="5" max="240" step="5" bind:value={isochroneMinutes} />
				</label>
				<div class="isochrone-prompt-buttons">
					<button type="button" onclick={() => (isochronePromptOpen = false)}>Cancel</button>
					<button type="button" class="primary" onclick={showIsochrone} disabled={isochroneLoading}>
						{isochroneLoading ? 'Loading…' : 'Show isochrone'}
					</button>
				</div>
				{#if isochroneError}
					<p class="isochrone-error">{isochroneError}</p>
				{/if}
			</div>
		{/if}
		{#if saveDialogOpen}
			<div class="dialog-backdrop">
				<form class="dialog" onsubmit={confirmSave}>
					<h3>Save route</h3>
					<!-- svelte-ignore a11y_autofocus -->
					<input bind:value={saveNameInput} autofocus maxlength="200" placeholder="Route name" />
					<div class="dialog-buttons">
						<button type="button" onclick={() => (saveDialogOpen = false)}>Cancel</button>
						<button type="submit" class="primary" disabled={saving || !saveNameInput.trim()}>
							Save
						</button>
					</div>
				</form>
			</div>
		{/if}
		{#if waypoints.length < 2}
			<div class="hint">Click the map to add waypoints. Drag the route to fine-tune it.</div>
		{/if}
		{#if error}
			<div class="banner error">{error}</div>
		{:else if loading}
			<div class="banner">Routing…</div>
		{/if}
	</div>
	{#if route}
		<div class="panel">
			<div class="stats">
				<span><strong>{formatDistance(route.distance_m)}</strong></span>
				<span title={route.ride_time.length ? 'Estimated for your rider profile' : undefined}>
					{formatDuration(
						route.ride_time.length
							? route.ride_time[route.ride_time.length - 1].time_s
							: route.duration_s
					)}
				</span>
				{#if route.ride_time.length}
					<a class="ride-time-link" href="/settings">rider profile</a>
				{/if}
				<span>↗ {Math.round(route.ascent_m)} m</span>
				<span>↘ {Math.round(route.descent_m)} m</span>
			</div>
			{#if route.surface && route.surface.total_m > 0}
				<SurfaceBar surface={route.surface} />
			{/if}
			<div class="panel-body">
				<ElevationProfile
					elevation={route.elevation}
					onHover={handleElevationHover}
					{hoveredClimb}
				/>
				<WaypointList
					{waypoints}
					searchEnabled={config?.search_enabled ?? false}
					hoveredIndex={hoveredWaypointIndex}
					onHover={(i) => (hoveredWaypointIndex = i)}
					onReorder={reorderWaypoint}
					onRemove={removeWaypoint}
				/>
				{#if route.climbs.length > 0}
					<ClimbsList
						climbs={route.climbs}
						hoveredIndex={hoveredClimbIndex}
						onHover={(i) => (hoveredClimbIndex = i)}
					/>
				{/if}
				{#if config?.search_enabled}
					<PoiPanel
						{pois}
						truncated={poisTruncated}
						loading={poisLoading}
						selected={poiGroups}
						hoveredId={hoveredPoiId}
						onToggleGroup={togglePoiGroup}
						onHover={(id) => (hoveredPoiId = id)}
					/>
				{/if}
				{#if config?.weather_enabled}
					<WeatherPanel
						startTime={weatherStartTime}
						onStartTimeChange={(value) => (weatherStartTime = value)}
						loading={windLoading}
						error={windError}
						segments={windSegments}
						truncated={windTruncated}
						onShowWind={showWind}
					/>
				{/if}
			</div>
		</div>
	{/if}
</div>

<style>
	.app {
		display: flex;
		flex-direction: column;
		height: 100%;
	}
	.map-area {
		position: relative;
		flex: 1;
		min-height: 0;
	}
	/* Its own row below the toolbar rather than centred beside it: the
	   toolbar grows as buttons appear (Save, GPX, FIT, Wahoo, the route
	   name), so anything sharing that line eventually collides with it. */
	.search-bar {
		position: absolute;
		top: 52px;
		left: 10px;
		z-index: 6;
	}

	.toolbar {
		position: absolute;
		top: 10px;
		left: 10px;
		display: flex;
		gap: 8px;
		align-items: center;
		flex-wrap: wrap;
		max-width: calc(100% - 20px);
		z-index: 5;
	}
	/* Anchors PresetSlidersPopover, which is positioned absolutely relative
	   to this wrapper rather than the whole toolbar. */
	.preset-anchor {
		position: relative;
	}
	@media (max-width: 640px) {
		.toolbar > button,
		.route-name {
			padding: 0.4rem 0.6rem;
			font-size: 0.85rem;
		}
	}
	.toolbar > button {
		border: 1px solid #ccc;
		background: #fff;
		border-radius: 8px;
		padding: 0.45rem 0.8rem;
		font: inherit;
		cursor: pointer;
	}
	.toolbar > button:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.toolbar > .save {
		background: #d33682;
		color: #fff;
		border-color: #d33682;
	}
	.toolbar > .wahoo {
		background: #268bd2;
		color: #fff;
		border-color: #268bd2;
	}
	.route-name {
		background: #fffffff0;
		border-radius: 8px;
		padding: 0.45rem 0.8rem;
		font-size: 0.9rem;
		color: #073642;
		box-shadow: 0 1px 4px #0002;
	}
	/* Below the toolbar and the search bar (which shares its top offset when
	   search is enabled), so the two rows never overlap. */
	.avoids {
		position: absolute;
		top: 96px;
		left: 10px;
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		z-index: 5;
	}
	.avoid-chip {
		display: flex;
		align-items: center;
		gap: 4px;
		background: #fffffff0;
		border: 1px solid #dc322f;
		border-radius: 8px;
		padding: 0.25rem 0.3rem 0.25rem 0.6rem;
		font-size: 0.8rem;
		color: #073642;
		box-shadow: 0 1px 4px #0002;
	}
	.avoid-remove {
		border: none;
		background: transparent;
		color: #dc322f;
		font-size: 0.95rem;
		line-height: 1;
		padding: 0.1rem 0.3rem;
		cursor: pointer;
	}
	.dialog-backdrop {
		position: absolute;
		inset: 0;
		background: #0003;
		display: flex;
		align-items: center;
		justify-content: center;
		z-index: 20;
	}
	.dialog {
		background: #fff;
		border-radius: 10px;
		padding: 1.2rem;
		width: min(320px, 85vw);
		display: flex;
		flex-direction: column;
		gap: 0.8rem;
		box-shadow: 0 8px 30px #0004;
	}
	.dialog h3 {
		margin: 0;
		font-size: 1rem;
		color: #073642;
	}
	.dialog input {
		font: inherit;
		padding: 0.5rem 0.6rem;
		border: 1px solid #ccc;
		border-radius: 6px;
	}
	.dialog-buttons {
		display: flex;
		justify-content: flex-end;
		gap: 0.6rem;
	}
	.dialog-buttons button {
		border: 1px solid #ccc;
		background: #fff;
		border-radius: 6px;
		padding: 0.4rem 0.9rem;
		font: inherit;
		cursor: pointer;
	}
	.dialog-buttons .primary {
		background: #d33682;
		border-color: #d33682;
		color: #fff;
	}
	.dialog-buttons .primary:disabled {
		opacity: 0.6;
	}
	/* A small card near the toolbar rather than a full dialog backdrop -
	   the isochrone origin is already marked on the map, so nothing needs
	   to be dimmed behind this. */
	.isochrone-prompt {
		position: absolute;
		top: 52px;
		right: 10px;
		background: #fff;
		border-radius: 10px;
		padding: 0.7rem 0.8rem;
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
		box-shadow: 0 4px 16px #0003;
		z-index: 6;
		font-size: 0.85rem;
	}
	.isochrone-prompt label {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.6rem;
	}
	.isochrone-prompt input {
		font: inherit;
		width: 4.5rem;
		padding: 0.3rem 0.4rem;
		border: 1px solid #ccc;
		border-radius: 6px;
	}
	.isochrone-prompt-buttons {
		display: flex;
		justify-content: flex-end;
		gap: 0.5rem;
	}
	.isochrone-prompt-buttons button {
		border: 1px solid #ccc;
		background: #fff;
		border-radius: 6px;
		padding: 0.35rem 0.8rem;
		font: inherit;
		cursor: pointer;
	}
	.isochrone-prompt-buttons .primary {
		background: #268bd2;
		border-color: #268bd2;
		color: #fff;
	}
	.isochrone-prompt-buttons .primary:disabled {
		opacity: 0.6;
	}
	.isochrone-error {
		margin: 0;
		color: #dc322f;
		font-size: 0.8rem;
	}
	.hint,
	.banner {
		position: absolute;
		left: 50%;
		transform: translateX(-50%);
		bottom: 18px;
		background: #fffffff0;
		border-radius: 8px;
		padding: 0.5rem 1rem;
		font-size: 0.9rem;
		box-shadow: 0 1px 4px #0002;
		z-index: 5;
	}
	.banner.error {
		background: #dc322f;
		color: #fff;
	}
	.panel {
		background: #fdf6e3;
		border-top: 1px solid #eee8d5;
		padding: 0.4rem 0.8rem 0.2rem;
	}
	/* Chart and POI list side by side on a desktop, stacked on a phone -
	   the chart needs width, the list needs height, and neither wins on a
	   narrow screen. */
	.panel-body {
		display: flex;
		gap: 1rem;
		align-items: flex-start;
	}
	.panel-body :global(.pois),
	.panel-body :global(.climbs),
	.panel-body :global(.weather),
	.panel-body :global(.waypoints) {
		flex: 0 0 21rem;
		min-width: 0;
	}
	@media (max-width: 760px) {
		.panel-body {
			flex-direction: column;
			gap: 0.2rem;
		}
		.panel-body :global(.pois),
		.panel-body :global(.climbs),
		.panel-body :global(.weather),
		.panel-body :global(.waypoints) {
			flex: none;
			width: 100%;
		}
	}
	.stats {
		display: flex;
		gap: 1.2rem;
		font-size: 0.95rem;
		padding: 0.2rem 0.2rem 0.4rem;
		color: #586e75;
	}
	.stats strong {
		color: #073642;
		font-size: 1.05rem;
	}
	.ride-time-link {
		font-size: 0.8rem;
		color: #268bd2;
		align-self: center;
		margin-left: -0.7rem;
	}
</style>
