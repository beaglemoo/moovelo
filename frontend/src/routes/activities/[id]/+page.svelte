<script lang="ts">
	import { page } from '$app/state';
	import {
		activities,
		fetchConfig,
		routes,
		type ActivityDetail,
		type RouteSummary,
		type SavedRoute
	} from '$lib/api';
	import ElevationProfile from '$lib/components/ElevationProfile.svelte';
	import MapView from '$lib/map/MapView.svelte';
	import { cumulativeDistances, pointAtDistance } from '$lib/geo';
	import { decodePolyline6 } from '$lib/polyline';
	import { distance, duration, elevation } from '$lib/format';
	import { units } from '$lib/units.svelte';

	const id = page.params.id ?? '';

	// Stable empty props for the read-only map - the share page's own
	// convention. Fresh inline literals would retrigger MapView's effects on
	// every render.
	const noWaypoints: never[] = [];
	const noLegStarts: never[] = [];
	const noop = () => {};

	let activity: ActivityDetail | null = $state(null);
	let matchedRoute: SavedRoute | null = $state(null);
	// Every one of the rider's own routes, for the manual picker. Loaded once
	// up front rather than searched - a rider's library is a few dozen
	// routes at most, not a few hundred.
	let routeOptions: RouteSummary[] = $state([]);
	let loading = $state(true);
	let error: string | null = $state(null);
	let cyclosmTileUrl: string | null = $state(null);
	let fitTrigger = $state(0);
	let hoverPoint: [number, number] | null = $state(null);
	let pickerBusy = $state(false);
	let pickerError: string | null = $state(null);

	fetchConfig().then((config) => (cyclosmTileUrl = config.tile_url_cyclosm));
	routes
		.list({ sort: 'name', order: 'asc' })
		.then((r) => (routeOptions = r))
		.catch(() => {
			/* the picker degrades to "no other routes to offer" rather than an error */
		});

	// `activity` and `matchedRoute` are two halves of one fact - the select is
	// bound to activity.route_id while every comparison row renders
	// matchedRoute - so they are fetched together and assigned together, after
	// both awaits have resolved. Assigning the ride first and then awaiting its
	// route is what let a failed second call leave the picker showing one route
	// and the comparison table showing another's distance, ascent and predicted
	// time. One commit point means the pair cannot diverge: on failure both are
	// left as they were, which is stale but coherent, and the error says so.
	async function fetchPair(id: string): Promise<[ActivityDetail, SavedRoute | null]> {
		const next = await activities.get(id);
		return [next, next.route_id ? await routes.get(next.route_id) : null];
	}

	async function load() {
		loading = true;
		error = null;
		try {
			[activity, matchedRoute] = await fetchPair(id);
			fitTrigger += 1;
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to load ride';
		} finally {
			loading = false;
		}
	}
	load();

	const routeLine = $derived.by(() => (activity ? decodePolyline6(activity.shape) : []));
	const routeDists = $derived(cumulativeDistances(routeLine));

	function onHover(distM: number | null) {
		hoverPoint = distM === null ? null : pointAtDistance(routeLine, routeDists, distM);
	}

	// The route's own predicted ride time is just the last point of its own
	// `ride_time` series - the same model api/routes.py already computes for
	// a saved route on every read, over the viewer's rider settings. Not a
	// second call: SavedRoute already carries it.
	const predictedTimeS = $derived.by(() =>
		matchedRoute && matchedRoute.ride_time.length
			? matchedRoute.ride_time[matchedRoute.ride_time.length - 1].time_s
			: null
	);

	async function pickRoute(event: Event) {
		if (!activity) return;
		const select = event.currentTarget as HTMLSelectElement;
		const value = select.value;
		pickerBusy = true;
		pickerError = null;
		try {
			const updated = await activities.setRoute(activity.id, value || null);
			// Resolved before either is assigned - see fetchPair's comment. A
			// null route_id needs no call, so clearing a link cannot fail here.
			const route = updated.route_id ? await routes.get(updated.route_id) : null;
			activity = updated;
			matchedRoute = route;
		} catch (err) {
			pickerError = err instanceof Error ? err.message : 'Failed to update the match';
			// The select is bound one way (`value={activity.route_id}`), so when
			// the state it reads does not change, nothing re-renders it and the
			// element keeps the choice the rider just made - leaving the picker
			// naming one route while the panel below describes another. Putting
			// it back by hand is what actually restores the pair on screen;
			// getting the state right is necessary but not sufficient, and the
			// difference is only visible in a browser, not in a state model.
			select.value = activity.route_id ?? '';
		} finally {
			pickerBusy = false;
		}
	}

	function rideDate(item: ActivityDetail): string {
		const stamp = item.started_at ?? item.created_at;
		const label = new Date(stamp).toLocaleDateString(undefined, {
			day: 'numeric',
			month: 'short',
			year: 'numeric'
		});
		return item.started_at ? label : `imported ${label}`;
	}
</script>

<svelte:head><title>{activity ? activity.name : 'Ride'} - Moovelo</title></svelte:head>

<div class="page">
	{#if loading}
		<p>Loading…</p>
	{:else if error}
		<p class="error">{error}</p>
	{:else if activity}
		<header class="head">
			<div>
				<h1>{activity.name}</h1>
				<span class="date">{rideDate(activity)}</span>
			</div>
			<a class="back" href="/activities">Back to activities</a>
		</header>

		<div class="stats">
			<span>{distance(activity.distance_m, units.system)}</span>
			<span>{elevation(activity.ascent_m, units.system)} ascent</span>
			<span>{duration(activity.moving_time_s)} moving</span>
			{#if activity.elapsed_time_s !== null}
				<span>{duration(activity.elapsed_time_s)} elapsed</span>
			{/if}
		</div>

		<div class="map">
			<MapView
				waypoints={noWaypoints}
				{routeLine}
				legStartIndices={noLegStarts}
				{hoverPoint}
				{cyclosmTileUrl}
				{fitTrigger}
				onAddWaypoint={noop}
				onMoveWaypoint={noop}
				onInsertVia={noop}
				onRemoveWaypoint={noop}
				onSetStart={noop}
				onSetEnd={noop}
				onClear={noop}
			/>
		</div>

		{#if activity.elevation.length > 1}
			<div class="profile">
				<ElevationProfile elevation={activity.elevation} {onHover} />
			</div>
		{/if}

		<section class="match">
			<h2>Matched route</h2>
			<label class="picker">
				<span>Route this ride followed</span>
				<select
					value={activity.route_id ?? ''}
					onchange={pickRoute}
					disabled={pickerBusy || routeOptions.length === 0}
				>
					<option value="">No matched route</option>
					{#each routeOptions as option (option.id)}
						<option value={option.id}>{option.name}</option>
					{/each}
				</select>
			</label>
			{#if pickerError}
				<p class="error">{pickerError}</p>
			{/if}

			{#if matchedRoute}
				<p class="route-link">
					<a href={`/library/${matchedRoute.id}`}>{matchedRoute.name}</a>
					{#if activity.match_confidence !== null}
						<span class="confidence">{Math.round(activity.match_confidence * 100)}% match</span>
					{:else}
						<span class="confidence">manual match</span>
					{/if}
				</p>

				<table class="comparison">
					<thead>
						<tr>
							<th></th>
							<th>Actual</th>
							<th>Planned</th>
						</tr>
					</thead>
					<tbody>
						<tr>
							<td>Time</td>
							<td>{duration(activity.moving_time_s)}</td>
							<td>{duration(predictedTimeS)}</td>
						</tr>
						<tr>
							<td>Distance</td>
							<td>{distance(activity.distance_m, units.system)}</td>
							<td>{distance(matchedRoute.distance_m, units.system)}</td>
						</tr>
						<tr>
							<td>Ascent</td>
							<td>{elevation(activity.ascent_m, units.system)}</td>
							<td>{elevation(matchedRoute.ascent_m, units.system)}</td>
						</tr>
					</tbody>
				</table>
			{:else}
				<p class="empty">No route matched to this ride yet.</p>
			{/if}
		</section>
	{/if}
</div>

<style>
	.page {
		max-width: 900px;
		margin: 0 auto;
		padding: 1.5rem 1rem 3rem;
	}
	.error {
		color: var(--danger-text);
	}
	.head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 1rem;
		flex-wrap: wrap;
	}
	h1 {
		font-size: 1.3rem;
		color: var(--text);
		margin: 0 0 0.2rem;
	}
	.date {
		color: var(--text-muted);
		font-size: 0.9rem;
	}
	.back {
		color: var(--link);
		font-size: 0.9rem;
		text-decoration: none;
	}
	.back:hover {
		text-decoration: underline;
	}
	.stats {
		display: flex;
		gap: 1rem;
		flex-wrap: wrap;
		color: var(--text-muted);
		font-size: 0.95rem;
		margin: 0.6rem 0 1rem;
	}
	.map {
		position: relative;
		height: 340px;
		border: 1px solid var(--border);
		border-radius: 8px;
		overflow: hidden;
		margin-bottom: 0.8rem;
	}
	.profile {
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 0.4rem 0.6rem;
		margin-bottom: 1.5rem;
		background: var(--surface);
	}
	.match h2 {
		font-size: 1.05rem;
		color: var(--text);
		margin: 0 0 0.6rem;
	}
	.picker {
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
		max-width: 320px;
		margin-bottom: 0.8rem;
		font-size: 0.9rem;
		color: var(--text-muted);
	}
	.picker select {
		font: inherit;
		padding: 0.35rem 0.5rem;
	}
	.route-link {
		margin: 0 0 0.8rem;
	}
	.route-link a {
		color: var(--link);
		font-weight: 600;
		text-decoration: none;
	}
	.route-link a:hover {
		text-decoration: underline;
	}
	.confidence {
		margin-left: 0.6rem;
		color: var(--text-muted);
		font-size: 0.85rem;
	}
	.empty {
		color: var(--text-muted);
	}
	table.comparison {
		width: 100%;
		max-width: 480px;
		border-collapse: collapse;
		font-size: 0.92rem;
	}
	table.comparison th {
		text-align: left;
		font-weight: 600;
		color: var(--text-muted);
		border-bottom: 1px solid var(--border);
		padding: 0.4rem 0.5rem;
	}
	table.comparison td {
		padding: 0.4rem 0.5rem;
		border-bottom: 1px solid var(--surface-sunken);
	}
</style>
