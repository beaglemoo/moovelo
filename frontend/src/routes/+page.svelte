<script lang="ts">
	import { page } from '$app/state';
	import type { FeatureCollection } from 'geojson';
	import {
		activities,
		fetchConfig,
		places,
		planRoute,
		routeAlternates,
		routeIsochrone,
		routeLoop,
		routeSurface,
		routeWeather,
		routes,
		suggestRouteName,
		wahoo,
		type AppConfig,
		type BicycleCostingOptions,
		type IsochroneResult,
		type AssistantProposal,
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
	import { distance, elevation } from '$lib/format';
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
	import AssistantPanel from '$lib/components/AssistantPanel.svelte';
	import PoiPanel from '$lib/components/PoiPanel.svelte';
	import PresetSelector from '$lib/components/PresetSelector.svelte';
	import PresetSlidersPopover from '$lib/components/PresetSlidersPopover.svelte';
	import SurfaceBar from '$lib/components/SurfaceBar.svelte';
	import WaypointList from '$lib/components/WaypointList.svelte';
	import WeatherPanel from '$lib/components/WeatherPanel.svelte';
	import MapView from '$lib/map/MapView.svelte';
	import { beforeNavigate } from '$app/navigation';
	import { units } from '$lib/units.svelte';
	import { unsaved } from '$lib/unsaved.svelte';
	import { onDestroy } from 'svelte';

	let waypoints: Waypoint[] = $state([]);
	let preset: Preset = $state('road');
	// Set when the "Custom" pill is active - overrides `preset` entirely for
	// routing, and is what makes a saved route's stored preset "custom".
	let customCostingOptions: BicycleCostingOptions | null = $state(null);
	let customPopoverOpen = $state(false);
	let route: RouteResponse | null = $state(null);
	// The reroute whose response still owns the route, or null when none is
	// outstanding. `loading` is derived from it rather than assigned, so it
	// cannot be left true by a path that returns early: claiming the route
	// (below) releases it, and every superseding action claims.
	let pendingRoute: number | null = $state(null);
	const loading = $derived(pendingRoute !== null);
	let error: string | null = $state(null);
	let hoverPoint: [number, number] | null = $state(null);
	let config: AppConfig | null = $state(null);
	// Whether this rider has any activities, so MapView can decide whether
	// the heatmap toggle is worth showing - a separate, per-user fetch
	// rather than a field on AppConfig, which the unauthenticated share page
	// also reads.
	let heatmapAvailable = $state(false);

	let fitTrigger = $state(0);
	let flyTo: Waypoint | null = $state(null);
	let flyTrigger = $state(0);
	let mapCentre: Waypoint | null = $state(null);
	let savedId: string | null = $state(null);
	let savedName: string | null = $state(null);
	let dirty = $state(false);
	// True whenever `route` no longer corresponds to `waypoints`. A failed
	// reroute leaves the previous geometry on screen deliberately - a blank
	// map on a transient 503 is worse than a stale line under an error
	// banner - but that pair must never be persisted: the stored track would
	// not match the stored waypoints, and nothing downstream cross-checks
	// them. `loading` alone does not cover this, because the catch that
	// leaves the state inconsistent resets `loading` on its way out.
	let routeStale = $state(false);
	// Ownership of `route`. Every path that puts a route on screen claims a
	// token first; an in-flight reroute whose token has been superseded throws
	// its response away instead of applying it. Cancellation used to be the
	// only defence and lived solely inside reroute(), so Clear, a chosen loop,
	// an adopted alternate and time travel each left a request running that
	// could land afterwards and resurrect geometry for waypoints that were no
	// longer on screen - undetectably, since the response set routeStale
	// false on its way in. A claim is checked at the point of use rather than
	// remembered at each call site, so a new path cannot forget it: the worst
	// a caller that forgets to claim can do is let its own route be replaced.
	let routeToken = 0;
	function claimRoute(): number {
		routeToken += 1;
		// Whatever was in flight no longer owns the route, so it no longer
		// owns the "Routing..." state either. Without this, a response dropped
		// for being superseded would leave the banner and every loading-gated
		// button stuck until reload.
		pendingRoute = null;
		return routeToken;
	}
	let saveDialogOpen = $state(false);
	let saveNameInput = $state('');
	let saveNotesInput = $state('');
	let saving = $state(false);
	let suggestingName = $state(false);
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
	let loopController: AbortController | null = null;
	// The avoids `loopCandidates` was fetched under, as a plain (non-$state)
	// snapshot - see the invalidation effect below. A candidate found before
	// an avoid existed routes straight through it, and "Use this loop" adopts
	// it verbatim.
	let loopFetchedFor: RoutingInputs | null = null;
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
	// The routing inputs `alternates` was fetched against, as a plain
	// (non-$state) snapshot - compared by value in the invalidation effect
	// below, so populating `alternates` itself never trips that effect's own
	// guard. Avoids belong here as much as waypoints do: they travel in the
	// alternates request, so a candidate fetched before an avoid existed
	// routes straight through it.
	let alternatesFetchedFor: RoutingInputs | null = null;

	// A route the assistant built and is offering. Held here rather than in
	// AssistantPanel so there is one source of truth for the preview line and
	// for the staleness check below; the panel renders the card and calls
	// back.
	let proposal: AssistantProposal | null = $state(null);
	// The routing inputs it was built under, as a plain (non-$state) variable
	// for the same reason loopFetchedFor is one - writing it must not
	// re-trigger the effect that reads it.
	let proposalFetchedFor: RoutingInputs | null = null;
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
	activities.heatmapAvailable().then((r) => (heatmapAvailable = r.available));

	// Bumped whenever the push in flight stops describing what is on screen.
	// A poll that outlives its generation must not write status: it would
	// resurrect "Sent to Wahoo" for a route that has since been edited.
	let wahooGeneration = 0;

	async function sendToWahoo() {
		// Same rule as export, and the same backstop-behind-a-disabled-button
		// pattern: the push sends whatever is STORED, so doing it with unsaved
		// edits on screen puts the previous version on the head unit.
		if (!savedId || dirty) return;
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

	// Leaving the planner with unsaved edits discards them: a nav link
	// unmounts it, and a ?route= load (opening a library route, an import
	// result's View) overwrites it in place. Only the file-drop path used to
	// check, so every other route out lost the work silently. Confirm before
	// any navigation away from a dirty planner - the client-side counterpart
	// of warnOnUnsaved's tab-close prompt, and the one guard the drop path now
	// relies on too rather than carrying its own copy.
	// A deliberate teardown (log out, or the forced bounce on an expired
	// session) sets unsaved.leaving before it navigates; the planner is being
	// abandoned on purpose, so guarding it just double-prompts. This is NOT a
	// /login-destination check: a browser Back to /login while still logged in
	// is an accidental exit that must still warn. Cleared here so the next
	// editing session is guarded again.
	unsaved.leaving = false;
	beforeNavigate((navigation) => {
		if (unsaved.leaving) return;
		if (!(dirty && waypoints.length > 0)) return;
		if (
			!confirm('You have unsaved changes to the route you are planning.\n\nLeave and lose them?')
		) {
			navigation.cancel();
		}
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
			savedId,
			savedName,
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
		// Restored exactly, null included - see PlannerSnapshot.savedId.
		// An accidental Clear stays fully undoable because clear() pushes its
		// snapshot BEFORE nulling savedId, so the entry undo lands on already
		// carries the library row and Save offers "Save changes".
		// Re-attaching only (never clearing) was tried and is worse: undoing
		// back past the point a route was saved would leave savedId pinned to
		// that row while the waypoints on screen wandered off to something
		// else entirely, and the next save would PUT the unrelated content
		// over it. Detaching can at worst duplicate a row, which the user can
		// see and delete; overwriting silently destroys a saved route.
		savedId = snap.savedId;
		savedName = snap.savedName;
		if (snap.routeOverride) {
			claimRoute();
			route = snap.routeOverride;
			routeStale = false;
			markEdited();
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
				claimRoute();
				route = saved;
				routeStale = false;
				savedId = saved.id;
				savedName = saved.name;
				savedSignature = savedStateSignature();
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

	const proposalPreview = $derived.by(() => {
		if (!proposal) return null;
		return mergeLegLines(proposal.snapshot.legs.map((leg) => decodePolyline6(leg.geometry)));
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
		const elevation = route?.elevation ?? null;
		surfaceController?.abort();
		if (legs.length === 0) {
			if (route) route.surface = null;
			surfacePending = null;
			return;
		}
		const controller = new AbortController();
		surfaceController = controller;
		// No preset/costing sent: an edge_walk trace follows the shape's own
		// edges, so costing cannot change the answer (it could only break
		// it) - see the backend's RouteSurfaceQuery. A preset change reroutes
		// anyway, which replaces the legs and re-fires this effect.
		surfacePending = routeSurface(legs, elevation, controller.signal)
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

	// reroute() is only ever called as the tail of an edit (the mutators via
	// planEdit, and time travel via applySnapshot), so every one of its exits
	// marks the planner dirty - not just the successful one. An edit that
	// routed badly, or that dropped below two waypoints, still means what is
	// stored no longer matches what is on screen.
	// What a save actually persists: the waypoints, the costing they were
	// planned with, and the geometry that came back. Two states with the same
	// signature are the same stored route.
	function savedStateSignature(): string {
		return JSON.stringify({
			waypoints,
			preset: storedPreset,
			costingOptions: customCostingOptions,
			geometry: route ? route.legs.map((leg) => leg.geometry) : null
		});
	}

	// The signature of what is in the library row, or null when nothing is
	// saved. Compared rather than trusted: `dirty` used to be a flag that no
	// path could clear, so undoing all the way back to the saved state still
	// counted as edited and left export and the Wahoo push disabled until the
	// rider saved again or reloaded.
	let savedSignature: string | null = null;

	function markEdited() {
		dirty = savedId === null || savedSignature === null || savedStateSignature() !== savedSignature;
	}

	async function reroute() {
		const token = claimRoute();
		abortController?.abort();
		if (waypoints.length < 2) {
			route = null;
			routeStale = false;
			error = null;
			markEdited();
			return;
		}
		abortController = new AbortController();
		pendingRoute = token;
		error = null;
		try {
			const next = await planRoute(
				waypoints,
				preset,
				customCostingOptions,
				avoids.length ? avoids : null,
				abortController.signal
			);
			// Anything that replaced the route while this was in flight - Clear,
			// a chosen loop, an adopted alternate, time travel - has claimed a
			// newer token, and this response now describes waypoints that are no
			// longer on screen. Dropping it is the whole point: aborting is a
			// courtesy that races, and every one of those callers would
			// otherwise have to remember to abort.
			if (token !== routeToken) return;
			pendingRoute = null;
			route = next;
			routeStale = false;
			markEdited();
		} catch (err) {
			if (err instanceof DOMException && err.name === 'AbortError') return;
			if (token !== routeToken) return;
			pendingRoute = null;
			routeStale = true;
			error = err instanceof Error ? err.message : 'Routing failed';
			markEdited();
		}
	}

	// Every ordinary planner edit is the same three steps around its one
	// mutation: confirm (imported routes), record for undo, re-route. One
	// wrapper so a future mutator cannot forget a step - forgetting
	// history.push in particular would silently break undo for that single
	// action while every sibling kept working.
	function planEdit(mutation: () => void) {
		if (!mayEdit()) return;
		history.push(currentSnapshot());
		mutation();
		reroute();
	}

	function addWaypoint(wp: Waypoint) {
		planEdit(() => waypoints.push(wp));
	}

	function moveWaypoint(index: number, wp: Waypoint) {
		planEdit(() => (waypoints[index] = wp));
	}

	function insertVia(position: number, wp: Waypoint) {
		planEdit(() => waypoints.splice(position, 0, wp));
	}

	function removeWaypoint(index: number) {
		planEdit(() => waypoints.splice(index, 1));
	}

	// Waypoint list panel: up/down buttons and native drag-and-drop both
	// funnel through this. Splice-out-splice-in rather than a swap, so
	// dragging a row several places in one go (not just adjacent, as the
	// buttons always do) still lands it exactly where it was dropped.
	function reorderWaypoint(from: number, to: number) {
		if (to < 0 || to >= waypoints.length || from === to) return;
		planEdit(() => {
			const [wp] = waypoints.splice(from, 1);
			waypoints.splice(to, 0, wp);
		});
	}

	// Gated by mayEdit() (inside planEdit) like every other mutator: an
	// avoid only means anything alongside waypoints to route between, and
	// adding one to an imported route's planner state is exactly the
	// "re-route this" edit mayEdit() exists to confirm.
	function addAvoid(wp: Waypoint) {
		planEdit(() => avoids.push(wp));
	}

	function removeAvoid(index: number) {
		planEdit(() => avoids.splice(index, 1));
	}

	function setStart(wp: Waypoint) {
		planEdit(() => {
			if (waypoints.length === 0) waypoints.push(wp);
			else waypoints[0] = wp;
		});
	}

	function setEnd(wp: Waypoint) {
		planEdit(() => {
			if (waypoints.length < 2) waypoints.push(wp);
			else waypoints[waypoints.length - 1] = wp;
		});
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

	// Undo/redo push the CURRENT state with the live route attached as
	// routeOverride (mutators never do - their route is about to be
	// replaced). Without it, redoing past an adopted alternate would replay
	// reroute() and silently come back with the primary route instead of
	// the alternate the rider picked.
	//
	// Only ever a route that matches the inputs beside it: mid-reroute, and
	// after one that failed, `route` still belongs to the PREVIOUS waypoints,
	// and restoring it verbatim would hand redo a mismatched pair - with
	// applySnapshot marking it clean, since a routeOverride is by definition
	// a route that matched when captured. Replaying reroute() is truer in
	// both cases: it either produces a route that really does match, or fails
	// and sets routeStale again.
	// The live route, but only when it genuinely describes the current inputs -
	// null while a reroute is in flight or after one failed, when it still
	// belongs to the previous waypoints. Anything storing a route for later
	// restoration goes through this: a routeOverride is taken on trust when it
	// comes back (applySnapshot marks it clean), so a stale one put into
	// history resurfaces as a mismatched pair that every guard believes.
	function restorableRoute(): RouteResponse | null {
		return loading || routeStale ? null : route;
	}

	function timeTravelSnapshot(): PlannerSnapshot {
		return { ...currentSnapshot(), routeOverride: restorableRoute() };
	}

	function undo() {
		const snap = history.undo(timeTravelSnapshot());
		if (snap) applySnapshot(snap);
	}

	function redo() {
		const snap = history.redo(timeTravelSnapshot());
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
		claimRoute();
		route = null;
		routeStale = false;
		error = null;
		savedId = null;
		savedName = null;
		savedSignature = null;
		dirty = false;
		hideIsochrone();
		dismissLoop();
		dismissAlternates();
	}

	function onLoop(wp: Waypoint) {
		// One results card at a time - LoopPanel and AlternatesPanel share the
		// same screen position, and stacking them hides whichever lost.
		dismissAlternates();
		loopOrigin = wp;
		loopCandidates = null;
		loopError = null;
		hoveredLoopIndex = null;
	}

	function dismissLoop() {
		loopController?.abort();
		loopOrigin = null;
		loopCandidates = null;
		loopError = null;
		loopLoading = false;
		hoveredLoopIndex = null;
		loopFetchedFor = null;
	}

	async function findLoops() {
		if (!loopOrigin) return;
		// Abort-on-supersede like every other fetch path here: without it a
		// slow search for a dismissed origin could land later and overwrite
		// the candidates of the origin now on screen.
		loopController?.abort();
		const controller = new AbortController();
		loopController = controller;
		loopLoading = true;
		loopError = null;
		loopCandidates = null;
		try {
			const result = await routeLoop(
				loopOrigin,
				loopTargetKm,
				preset,
				customCostingOptions,
				avoids.length ? avoids : null,
				controller.signal
			);
			if (controller.signal.aborted) return;
			loopCandidates = result.candidates;
			loopFetchedFor = currentRoutingInputs();
		} catch (err) {
			if (controller.signal.aborted) return;
			loopError = err instanceof Error ? err.message : 'Loop search failed';
		} finally {
			if (loopController === controller) loopLoading = false;
		}
	}

	function sameWaypoints(a: Waypoint[], b: Waypoint[]): boolean {
		return a.length === b.length && a.every((wp, i) => wp.lat === b[i].lat && wp.lon === b[i].lon);
	}

	// Both results panels hold routes fetched under the inputs of the moment,
	// and both are adopted verbatim when the rider picks one - so both have to
	// be dropped the moment those inputs move on. They are checked together
	// here rather than one each: invalidating one panel while leaving the other
	// alone is a bug this subsystem has now produced twice.
	//
	// The comparison covers EVERY routing input, not just the ones a given
	// call site happens to change. Tracking waypoints and avoids alone missed
	// undo/redo: applySnapshot restores `preset` and `customCostingOptions`
	// directly, so time-travelling across a preset change with identical
	// waypoints left a panel open whose candidates were costed under the
	// preset being undone - and adopting one saved a library row declaring
	// "road" over geometry Valhalla drew for gravel. changePreset() and
	// changeCustomCosting() still dismiss eagerly for immediate feedback;
	// this is the backstop that covers every path that does not.
	$effect(() => {
		// Copied, not just referenced: `waypoints` and `avoids` are mutated in
		// place (push/splice), so reading the binding alone subscribes to
		// reassignment and nothing else. Mapping reads the length and every
		// element, which is what actually registers the dependency - and it
		// has to happen here, before the guards below, or a null fetched-for
		// snapshot short-circuits past the read and the effect never wakes up
		// again. Those snapshots are plain variables, so setting one cannot
		// re-trigger this either.
		const current = currentRoutingInputs();
		if (alternatesFetchedFor && !sameRoutingInputs(current, alternatesFetchedFor, true)) {
			dismissAlternates();
		}
		// Loops are anchored to their own origin rather than the waypoints
		// they will replace, so the waypoints are excluded for them - every
		// other input still applies.
		if (loopFetchedFor && !sameRoutingInputs(current, loopFetchedFor, false)) {
			dismissLoop();
		}
		// Waypoints count for a proposal, unlike a loop: a loop is anchored to
		// its own origin, but a proposal is an offer to replace what is on
		// screen, and once the rider has moved on it is describing a route
		// they are no longer looking at.
		if (proposalFetchedFor && !sameRoutingInputs(current, proposalFetchedFor, true)) {
			dismissProposal();
		}
	});

	/** Everything that changes what a route request would come back with. */
	interface RoutingInputs {
		waypoints: Waypoint[];
		avoids: Waypoint[];
		preset: Preset;
		costingOptions: BicycleCostingOptions | null;
	}

	function currentRoutingInputs(): RoutingInputs {
		return {
			waypoints: waypoints.map((wp) => ({ ...wp })),
			avoids: avoids.map((wp) => ({ ...wp })),
			preset,
			costingOptions: customCostingOptions ? { ...customCostingOptions } : null
		};
	}

	function sameRoutingInputs(
		a: RoutingInputs,
		b: RoutingInputs,
		compareWaypoints: boolean
	): boolean {
		if (compareWaypoints && !sameWaypoints(a.waypoints, b.waypoints)) return false;
		if (!sameWaypoints(a.avoids, b.avoids)) return false;
		if (a.preset !== b.preset) return false;
		return JSON.stringify(a.costingOptions) === JSON.stringify(b.costingOptions);
	}

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
		// One results card at a time - see onLoop.
		dismissLoop();
		alternatesOpen = true;
		alternatesLoading = true;
		alternatesError = null;
		alternates = null;
		try {
			// Avoids ride along: alternates that route straight through an
			// avoided road would be adoptable lies.
			const result = await routeAlternates(
				[waypoints[0], waypoints[1]],
				preset,
				customCostingOptions,
				avoids.length ? avoids : null
			);
			alternates = result.alternates;
			alternatesFetchedFor = currentRoutingInputs();
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
		history.push({ ...currentSnapshot(), routeOverride: restorableRoute() });
		claimRoute();
		route = picked;
		routeStale = false;
		markEdited();
		dismissAlternates();
	}

	function useLoop(candidate: LoopCandidate) {
		if (!mayEdit()) return;
		// An ordinary input change as far as undo is concerned: the pre-loop
		// waypoints/preset are what should come back.
		history.push(currentSnapshot());
		// Valhalla already ran for this candidate - reroute() would replan it
		// from scratch for no reason, so the snapshot goes straight onto
		// `route` instead, marking the planner edited exactly as reroute()
		// itself does for every other edit.
		waypoints = candidate.waypoints;
		claimRoute();
		route = candidate.snapshot;
		routeStale = false;
		markEdited();
		dismissLoop();
		fitTrigger += 1;
	}

	function offerProposal(offered: AssistantProposal) {
		proposal = offered;
		proposalFetchedFor = currentRoutingInputs();
	}

	function dismissProposal() {
		proposal = null;
		proposalFetchedFor = null;
	}

	function applyProposal() {
		if (!proposal || !mayEdit()) return;
		// An ordinary input change as far as undo is concerned, exactly like
		// useLoop: the pre-proposal waypoints and preset are what should come
		// back.
		history.push(currentSnapshot());
		// The assistant already routed this through Valhalla, so the snapshot
		// goes straight onto `route` rather than being replanned.
		waypoints = proposal.waypoints;
		preset = proposal.preset;
		// The proposal carries a named preset and its geometry was routed under
		// that preset, so any custom bundle the rider had dialled in earlier no
		// longer describes this route - clear it, exactly as changePreset does.
		// Left set, storedPreset would compute 'custom' and a save would persist
		// preset='custom' plus the stale bundle against road-preset geometry,
		// which a later reverse or recompute then mis-costs.
		customCostingOptions = null;
		claimRoute();
		route = proposal.snapshot;
		routeStale = false;
		markEdited();
		dismissProposal();
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

	/** The save dialog's explicit "Suggest" button - an assistant call the
	 * rider asked for, unlike suggestName() above which fills the box
	 * automatically from the place index alone. Fails silently: whatever is
	 * already in the box is left exactly as it was, same as suggestName()
	 * finding no place names. */
	async function suggestNameFromAssistant() {
		// Same `loading`/`routeStale` guard as save(), for the same reason: this
		// reads `route.distance_m`/`route.ascent_m` and `waypoints` together, and
		// mid-reroute (or after a failed one, where `loading` is already back to
		// false) those two disagree. The result would be a name built from the
		// new waypoints' place names and the old route's numbers - wrong in a way
		// the rider cannot see, since a plausible name is exactly what they
		// expected. The button is disabled as well; this is the backstop.
		if (!config?.assistant_enabled || !route || waypoints.length < 2) return;
		if (loading || routeStale) return;
		suggestingName = true;
		try {
			saveNameInput = await suggestRouteName(waypoints, route.distance_m, route.ascent_m);
		} catch {
			// Leave the input untouched - the rider can still type a name or
			// keep the date-based fallback.
		} finally {
			suggestingName = false;
		}
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
		// Never while `route` and `waypoints` disagree: mid-reroute (`loading`)
		// the geometry is still the pre-edit one, and after a FAILED reroute
		// it stays that way with `loading` already back to false - which is
		// the likelier click, since the user is looking at an error banner.
		// Saving either pair persists a route whose stored track does not
		// match its stored waypoints, and nothing downstream cross-checks the
		// two. The button is disabled as well; this is the backstop.
		if (!route || waypoints.length < 2 || loading || routeStale) return;
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
			const snapshot = route;
			if (!snapshot) return;
			// Re-checked here, not only at entry: the wait above chases the
			// surface refetch a completed reroute starts, but an edit made
			// during it whose reroute has NOT landed leaves `route` on the
			// pre-edit geometry while `waypoints` already holds the edit.
			// Nothing stops the rider editing the map while a save is in
			// flight, and the server cross-checks neither - so this window
			// persisted a row whose stored track belonged to different
			// waypoints. Reproduced live: waypoints of 4 against 2 legs.
			if (loading || routeStale || waypoints.length < 2) return;
			const persisted = savedStateSignature();
			await routes.update(savedId, {
				waypoints,
				preset: storedPreset,
				costing_options: customCostingOptions,
				snapshot,
				notes: saveNotesInput.trim() || null
			});
			savedSignature = persisted;
			// Compared, not assumed: an edit that landed during the round trip
			// leaves the screen differing from what was just stored, and one
			// that landed and was undone again does not. This replaces an
			// edit-generation counter that could only ever answer "did
			// anything happen", never "does it still differ".
			dirty = savedStateSignature() !== savedSignature;
		} catch (err) {
			error = err instanceof Error ? err.message : 'Save failed';
		} finally {
			saving = false;
		}
	}

	// Exports come from the stored route, so unsaved edits would silently
	// download the previous version. The Wahoo push is the same operation
	// aimed at a head unit rather than a file, so it shares the gate: pushing
	// while edits are on screen sends the rider out on the ride they can see
	// they are not looking at.
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
		// Same route-matches-waypoints guard as save() - see the comment there.
		if (!route || !saveNameInput.trim() || loading || routeStale) return;
		saving = true;
		try {
			await awaitSurfaceSettled();
			// See save(): captured after the settle wait, beside the
			// snapshot read - a mid-wait edit is in the snapshot and saved.
			const snapshot = route;
			if (!snapshot) return;
			// See save(): an edit made during the wait whose reroute has not
			// landed leaves route and waypoints describing different rides.
			if (loading || routeStale || waypoints.length < 2) return;
			const persisted = savedStateSignature();
			const saved = await routes.create({
				name: saveNameInput.trim(),
				waypoints,
				preset: storedPreset,
				costing_options: customCostingOptions,
				snapshot
			});
			savedId = saved.id;
			savedName = saved.name;
			savedSignature = persisted;
			// See save(): compared rather than assumed.
			dirty = savedStateSignature() !== savedSignature;
			saveDialogOpen = false;
			saveNotesInput = '';
		} catch (err) {
			error = err instanceof Error ? err.message : 'Save failed';
		} finally {
			saving = false;
		}
	}

	// The costing counterpart of planEdit(): a costing change re-routes the
	// line between its endpoints exactly as a waypoint edit does, so it must
	// go through the same mayEdit() confirmation - for an imported route that
	// reroute discards the imported track and its cues. changePreset and
	// changeCustomCosting were the two mutators that rerouted with no confirm,
	// so this wrapper retires the family rather than adding the guard twice.
	// It also dismisses the panels a costing change makes stale, which the
	// waypoint-only planEdit does not.
	function costingEdit(mutation: () => void) {
		if (!mayEdit()) return;
		history.push(currentSnapshot());
		mutation();
		// Anything computed under the old costing is stale the moment the
		// costing changes - leaving the panels open would let a road-costed
		// alternate or loop be adopted while the toolbar says gravel.
		dismissAlternates();
		dismissLoop();
		reroute();
	}

	function changePreset(next: Preset) {
		costingEdit(() => {
			preset = next;
			customCostingOptions = null;
		});
	}

	function changeCustomCosting(next: BicycleCostingOptions) {
		costingEdit(() => (customCostingOptions = next));
	}

	function handleElevationHover(distM: number | null) {
		hoverPoint = distM === null ? null : pointAtDistance(routeLine, routeDists, distM);
	}

	function formatDuration(s: number): string {
		const h = Math.floor(s / 3600);
		const min = Math.round((s % 3600) / 60);
		return h ? `${h} h ${min.toString().padStart(2, '0')} min` : `${min} min`;
	}

	// The toolbar's own top offset (10px) and the search bar's (52px) below
	// it assumed a single-row toolbar. The toolbar grows buttons as a route
	// is planned and saved (Save, GPX, FIT, the route name, Wahoo) and wraps
	// onto a second row on anything narrower than a wide desktop - at which
	// point a fixed 52px sits inside the wrapped row rather than below it,
	// and the search bar renders on top of it. Measuring the real rendered
	// height keeps the stack (toolbar -> search bar -> avoid chips) in flow
	// regardless of how many rows the toolbar wraps to. Defaults match the
	// old fixed offsets, so the first paint looks the same until measured.
	let toolbarEl = $state<HTMLDivElement | undefined>();
	let searchBarEl = $state<HTMLDivElement | undefined>();
	let toolbarHeight = $state(32);
	let searchBarHeight = $state(34);
	const STACK_TOP = 10;
	const STACK_GAP = 10;
	// Wrapped in its own function rather than inlined into the $derived
	// below: TypeScript narrows `config` to its initializer's type (`null`)
	// at the top level of the component's setup code, so `config?.search_enabled`
	// written directly there type-errors as a property access on `never`. A
	// function body is checked in its own scope and does not inherit that.
	function isSearchEnabled(): boolean {
		return config?.search_enabled ?? false;
	}
	const searchBarTop = $derived(STACK_TOP + toolbarHeight + STACK_GAP);
	const searchEnabled = $derived(isSearchEnabled());
	const avoidsTop = $derived(
		searchEnabled ? searchBarTop + searchBarHeight + STACK_GAP : searchBarTop
	);

	// $effect, not onMount: the search bar is conditional on config (loaded
	// asynchronously), so searchBarEl does not exist on the very first
	// render. An effect re-subscribes whenever the bound element itself
	// changes, including from undefined to mounted.
	$effect(() => {
		if (toolbarEl) {
			const observer = new ResizeObserver((entries) => {
				const height = entries[0]?.contentRect.height;
				if (height) toolbarHeight = height;
			});
			observer.observe(toolbarEl);
			return () => observer.disconnect();
		}
	});

	$effect(() => {
		if (searchBarEl) {
			const observer = new ResizeObserver((entries) => {
				const height = entries[0]?.contentRect.height;
				if (height) searchBarHeight = height;
			});
			observer.observe(searchBarEl);
			return () => observer.disconnect();
		}
	});
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
				{heatmapAvailable}
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
				{proposalPreview}
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
			<div class="search-bar" bind:this={searchBarEl} style="top: {searchBarTop}px">
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
		<!-- One call site, deliberately. Rendering this from two branches of
		     one conditional made Svelte treat it as two components, so `route`
		     going from null to non-null - accepting a proposal, drawing a
		     route, loading a saved one - destroyed the panel and mounted an
		     empty one, taking the whole conversation with it. As a floating
		     overlay it no longer needs to branch on `route` at all: it lives
		     in .map-area alongside the other overlays, not in the docked
		     panel, so it renders whenever the assistant is configured. -->
		{#if config?.assistant_enabled}
			<AssistantPanel
				{waypoints}
				centre={mapCentre}
				{preset}
				costingOptions={customCostingOptions}
				{proposal}
				onProposal={offerProposal}
				onAccept={applyProposal}
				onDiscard={dismissProposal}
			/>
		{/if}
		<div class="toolbar" bind:this={toolbarEl}>
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
				disabled={waypoints.length !== 2 || loading || routeStale}
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
				disabled={!route || saving || loading || routeStale || (savedId !== null && !dirty)}
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
					disabled={wahooPush === 'working' || !canExport}
					title={wahooPush === 'error'
						? (wahooPushError ?? '')
						: dirty
							? 'Save your changes first - the push comes from the saved route'
							: 'Push to your Wahoo account'}
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
			<div class="avoids" role="group" aria-label="Avoided roads" style="top: {avoidsTop}px">
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
					<input type="number" min="5" max="120" step="5" bind:value={isochroneMinutes} />
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
					<div class="dialog-name-row">
						<!-- svelte-ignore a11y_autofocus -->
						<input bind:value={saveNameInput} autofocus maxlength="200" placeholder="Route name" />
						{#if config?.assistant_enabled}
							<button
								type="button"
								class="suggest-name"
								onclick={suggestNameFromAssistant}
								disabled={suggestingName || !route || loading || routeStale}
								title="Ask the assistant to suggest a name"
							>
								{suggestingName ? '…' : 'Suggest'}
							</button>
						{/if}
					</div>
					<label class="dialog-notes">
						Notes
						<textarea
							bind:value={saveNotesInput}
							rows="2"
							maxlength="5000"
							placeholder="Anything worth remembering about this ride"
						></textarea>
					</label>
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
				<span><strong>{distance(route.distance_m, units.system)}</strong></span>
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
				<span>↗ {elevation(route.ascent_m, units.system)}</span>
				<span>↘ {elevation(route.descent_m, units.system)}</span>
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
	/* The floor is load-bearing, not cosmetic. The panel below grows a card
	   per feature - elevation, stats, POIs, climbs, waypoints, the assistant -
	   and with flex: 1 alone the map is whatever is left over: measured at
	   204px on a 720px laptop with two cards open, which is barely taller
	   than the floating controls sitting on top of it. */
	.map-area {
		position: relative;
		flex: 1;
		min-height: 260px;
	}
	/* Its own row below the toolbar rather than centred beside it: the
	   toolbar grows as buttons appear (Save, GPX, FIT, Wahoo, the route
	   name), so anything sharing that line eventually collides with it.
	   `top` is set inline from a measured toolbar height (see searchBarTop
	   in the script), not a fixed offset: a fixed 52px assumed one row, and
	   the toolbar wraps to two or three on anything narrower than a wide
	   desktop once a route is saved, which used to put this bar on top of
	   the wrapped row instead of below it. */
	.search-bar {
		position: absolute;
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
		border: 1px solid var(--input-border);
		background: var(--surface);
		color: var(--text);
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
		/* Route-brand magenta, same literal MapView uses for the route line -
		   deliberately not tokenised, see step 3. */
		background: #d33682;
		color: #fff;
		border-color: #d33682;
	}
	.toolbar > .wahoo {
		background: var(--accent-fill);
		color: #fff;
		border-color: var(--accent);
	}
	.route-name {
		background: var(--surface);
		border-radius: 8px;
		padding: 0.45rem 0.8rem;
		font-size: 0.9rem;
		color: var(--text);
		box-shadow: 0 1px 4px var(--shadow);
	}
	/* Below the toolbar, and below the search bar too when it is enabled -
	   `top` is set inline from avoidsTop (see the script), which stacks on
	   the same measured heights as the search bar rather than a fixed
	   offset, for the same reason. */
	.avoids {
		position: absolute;
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
		background: var(--surface);
		border: 1px solid var(--danger);
		border-radius: 8px;
		padding: 0.25rem 0.3rem 0.25rem 0.6rem;
		font-size: 0.8rem;
		color: var(--text);
		box-shadow: 0 1px 4px var(--shadow);
	}
	.avoid-remove {
		border: none;
		background: transparent;
		color: var(--danger-text);
		font-size: 0.95rem;
		line-height: 1;
		padding: 0.1rem 0.3rem;
		cursor: pointer;
	}
	.dialog-backdrop {
		position: absolute;
		inset: 0;
		background: var(--overlay-scrim);
		display: flex;
		align-items: center;
		justify-content: center;
		z-index: 20;
	}
	.dialog {
		background: var(--surface);
		border-radius: 10px;
		padding: 1.2rem;
		width: min(320px, 85vw);
		display: flex;
		flex-direction: column;
		gap: 0.8rem;
		box-shadow: 0 8px 30px var(--shadow);
	}
	.dialog h3 {
		margin: 0;
		font-size: 1rem;
		color: var(--text);
	}
	.dialog input {
		font: inherit;
		padding: 0.5rem 0.6rem;
		border: 1px solid var(--input-border);
		background: var(--input-bg);
		color: var(--text);
		border-radius: 6px;
	}
	.dialog-name-row {
		display: flex;
		gap: 0.5rem;
	}
	.dialog-name-row input {
		flex: 1;
		min-width: 0;
	}
	.dialog-name-row .suggest-name {
		border: 1px solid var(--input-border);
		background: var(--surface);
		color: var(--text);
		border-radius: 6px;
		padding: 0.4rem 0.7rem;
		font: inherit;
		cursor: pointer;
		white-space: nowrap;
	}
	.dialog-name-row .suggest-name:disabled {
		opacity: 0.6;
		cursor: default;
	}
	.dialog-notes {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		font-size: 0.85rem;
		color: var(--text-muted);
	}
	.dialog-notes textarea {
		font: inherit;
		padding: 0.4rem 0.5rem;
		border: 1px solid var(--input-border);
		background: var(--input-bg);
		color: var(--text);
		border-radius: 6px;
		resize: vertical;
	}
	.dialog-buttons {
		display: flex;
		justify-content: flex-end;
		gap: 0.6rem;
	}
	.dialog-buttons button {
		border: 1px solid var(--input-border);
		background: var(--surface);
		color: var(--text);
		border-radius: 6px;
		padding: 0.4rem 0.9rem;
		font: inherit;
		cursor: pointer;
	}
	.dialog-buttons .primary {
		/* Route-brand magenta - deliberately not tokenised, see step 3. */
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
		background: var(--surface);
		border-radius: 10px;
		padding: 0.7rem 0.8rem;
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
		box-shadow: 0 4px 16px var(--shadow);
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
		border: 1px solid var(--input-border);
		background: var(--input-bg);
		color: var(--text);
		border-radius: 6px;
	}
	.isochrone-prompt-buttons {
		display: flex;
		justify-content: flex-end;
		gap: 0.5rem;
	}
	.isochrone-prompt-buttons button {
		border: 1px solid var(--input-border);
		background: var(--surface);
		color: var(--text);
		border-radius: 6px;
		padding: 0.35rem 0.8rem;
		font: inherit;
		cursor: pointer;
	}
	.isochrone-prompt-buttons .primary {
		background: var(--accent-fill);
		border-color: var(--accent);
		color: #fff;
	}
	.isochrone-prompt-buttons .primary:disabled {
		opacity: 0.6;
	}
	.isochrone-error {
		margin: 0;
		color: var(--danger-text);
		font-size: 0.8rem;
	}
	.hint,
	.banner {
		position: absolute;
		left: 50%;
		transform: translateX(-50%);
		bottom: 18px;
		background: var(--surface);
		color: var(--text);
		border-radius: 8px;
		padding: 0.5rem 1rem;
		font-size: 0.9rem;
		box-shadow: 0 1px 4px var(--shadow);
		z-index: 5;
	}
	.banner.error {
		background: var(--danger);
		color: #fff;
	}
	/* Paired with .map-area's floor: the panel yields first and scrolls
	   internally, rather than pushing the map out of the viewport. */
	.panel {
		background: var(--surface);
		border-top: 1px solid var(--border);
		padding: 0.4rem 0.8rem 0.2rem;
		max-height: 55vh;
		overflow-y: auto;
	}
	/* Chart and POI list side by side on a desktop, stacked on a phone -
	   the chart needs width, the list needs height, and neither wins on a
	   narrow screen. */
	.panel-body {
		display: flex;
		gap: 1rem;
		align-items: flex-start;
	}
	/* The side panels are pinned at a fixed basis, so without this the
	   elevation chart is the only flexible child and absorbs every pixel of
	   overflow - collapsing to nothing on an ordinary laptop once four or
	   five panels are open. It wraps instead now. */
	.panel-body > :global(svg) {
		flex: 1 1 22rem;
		min-width: 18rem;
	}
	@media (min-width: 761px) {
		.panel-body {
			flex-wrap: wrap;
		}
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
		/* Stacked, the panel-body is a column, so the chart's `flex: 1 1 22rem`
		   basis becomes its HEIGHT - inflating a 150px chart to 352px+ and
		   pushing the map down to its floor so there is nowhere left to tap in
		   waypoints. Reset it: the chart falls back to its own 150px height. */
		.panel-body > :global(svg) {
			flex: none;
			width: 100%;
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
		color: var(--text-muted);
	}
	.stats strong {
		color: var(--text);
		font-size: 1.05rem;
	}
	.ride-time-link {
		font-size: 0.8rem;
		color: var(--link);
		align-self: center;
		margin-left: -0.7rem;
	}
</style>
