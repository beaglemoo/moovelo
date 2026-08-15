<script lang="ts">
	import { page } from '$app/state';
	import {
		fetchConfig,
		routes,
		type RouteActivity,
		type RouteActivitiesResponse,
		type SavedRoute
	} from '$lib/api';
	import ElevationProfile from '$lib/components/ElevationProfile.svelte';
	import MapView from '$lib/map/MapView.svelte';
	import { cumulativeDistances, mergeLegLines, pointAtDistance } from '$lib/geo';
	import { decodePolyline6 } from '$lib/polyline';
	import { distance, duration, elevation } from '$lib/format';
	import { units } from '$lib/units.svelte';

	// $derived, not a one-time read: SvelteKit reuses this component when one
	// /library/[id] navigates to another, so a captured id leaves the page
	// showing the previous route under the new URL.
	const id = $derived(page.params.id ?? '');

	// Stable empty props for the read-only map, matching the share page's own
	// convention - fresh inline literals would retrigger MapView's effects on
	// every render.
	const noWaypoints: never[] = [];
	const noLegStarts: never[] = [];
	const noop = () => {};

	let route: SavedRoute | null = $state(null);
	let matched: RouteActivitiesResponse | null = $state(null);
	let loading = $state(true);
	let error: string | null = $state(null);
	let cyclosmTileUrl: string | null = $state(null);
	let fitTrigger = $state(0);
	let hoverPoint: [number, number] | null = $state(null);

	fetchConfig().then((config) => (cyclosmTileUrl = config.tile_url_cyclosm));

	// Every load owns a token, and a superseded one applies nothing. Two
	// quick navigations race otherwise: the first route's response can land
	// after the second's and overwrite it, leaving the older route on screen
	// under the newer URL - the same ownership mechanism the planner uses on
	// `route` for the identical reason.
	let loadToken = 0;

	async function load(target: string) {
		if (!target) return;
		const token = ++loadToken;
		loading = true;
		error = null;
		try {
			const [nextRoute, nextMatched] = await Promise.all([
				routes.get(target),
				routes.activities(target)
			]);
			if (token !== loadToken) return;
			route = nextRoute;
			matched = nextMatched;
			fitTrigger += 1;
		} catch (err) {
			if (token !== loadToken) return;
			error = err instanceof Error ? err.message : 'Failed to load route';
		} finally {
			if (token === loadToken) loading = false;
		}
	}

	$effect(() => {
		load(id);
	});

	const routeLine = $derived.by(() =>
		route ? mergeLegLines(route.legs.map((leg) => decodePolyline6(leg.geometry))) : []
	);
	const routeDists = $derived(cumulativeDistances(routeLine));

	function onHover(distM: number | null) {
		hoverPoint = distM === null ? null : pointAtDistance(routeLine, routeDists, distM);
	}

	function rideDate(item: RouteActivity): string | null {
		return item.started_at
			? new Date(item.started_at).toLocaleDateString(undefined, {
					day: 'numeric',
					month: 'short',
					year: 'numeric'
				})
			: null;
	}
</script>

<svelte:head><title>{route ? route.name : 'Route'} - Moovelo</title></svelte:head>

<div class="page">
	{#if loading}
		<p>Loading…</p>
	{:else if error}
		<p class="error">{error}</p>
	{:else if route && matched}
		<header class="head">
			<div>
				<h1>{route.name}</h1>
				<span class="preset">{route.preset}</span>
			</div>
			<a class="back" href="/library">Back to library</a>
		</header>

		<div class="stats">
			<span>{distance(route.distance_m, units.system)}</span>
			<span>{elevation(route.ascent_m, units.system)} ascent</span>
			<span>{duration(matched.predicted_time_s)} predicted</span>
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

		{#if route.elevation.length > 1}
			<div class="profile">
				<ElevationProfile elevation={route.elevation} {onHover} />
			</div>
		{/if}

		<section class="rides">
			<h2>Rides of this route</h2>
			{#if matched.activities.length === 0}
				<p class="empty">
					No rides matched to this route yet. Import a GPX, TCX or FIT file from a head unit or a
					Strava export on the <a href="/activities">activities</a> page, and a ride that closely follows
					this route's line will link here automatically.
				</p>
			{:else}
				<table>
					<thead>
						<tr>
							<th>Date</th>
							<th>Ride</th>
							<th>Time</th>
							<th>Distance</th>
							<th>Ascent</th>
							<th>Match</th>
						</tr>
					</thead>
					<tbody>
						{#each matched.activities as item (item.id)}
							<tr>
								<td data-label="Date">{rideDate(item) ?? '-'}</td>
								<td data-label="Ride">
									<a class="ride-name" href={`/activities/${item.id}`}>{item.name}</a>
								</td>
								<td data-label="Time">
									{duration(item.moving_time_s)}
									<span class="vs">vs {duration(matched.predicted_time_s)} planned</span>
								</td>
								<td data-label="Distance">
									{distance(item.distance_m, units.system)}
									<span class="vs">vs {distance(route.distance_m, units.system)} planned</span>
								</td>
								<td data-label="Ascent">
									{elevation(item.ascent_m, units.system)}
									<span class="vs">vs {elevation(route.ascent_m, units.system)} planned</span>
								</td>
								<td data-label="Match">
									{#if item.match_stale}
										<span
											class="stale"
											title="This route was edited after the ride was matched to it, so the comparison is against a shape the ride never followed."
										>
											before edit
										</span>
									{:else if item.match_confidence !== null}
										{Math.round(item.match_confidence * 100)}%
									{:else}
										<span class="muted">manual</span>
									{/if}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
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
	.preset {
		color: var(--text-muted);
		font-size: 0.9rem;
		text-transform: capitalize;
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
	.rides h2 {
		font-size: 1.05rem;
		color: var(--text);
		margin: 0 0 0.6rem;
	}
	.empty {
		color: var(--text-muted);
		max-width: 56ch;
		line-height: 1.5;
	}
	.empty a {
		color: var(--link);
	}
	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.92rem;
	}
	th {
		text-align: left;
		font-weight: 600;
		color: var(--text-muted);
		border-bottom: 1px solid var(--border);
		padding: 0.4rem 0.5rem;
	}
	td {
		padding: 0.5rem;
		border-bottom: 1px solid var(--surface-sunken);
		vertical-align: top;
	}
	.ride-name {
		font-weight: 600;
		color: var(--link);
		text-decoration: none;
	}
	.ride-name:hover {
		text-decoration: underline;
	}
	.vs {
		display: block;
		color: var(--text-muted);
		font-size: 0.8rem;
	}
	.muted {
		color: var(--text-muted);
	}
	.stale {
		color: var(--warning-text);
		border-bottom: 1px dotted currentColor;
		cursor: help;
	}
	@media (max-width: 640px) {
		table,
		thead,
		tbody,
		tr,
		td {
			display: block;
		}
		thead {
			display: none;
		}
		tr {
			border-bottom: 1px solid var(--border);
			padding: 0.4rem 0;
		}
		td {
			border-bottom: none;
			padding: 0.15rem 0;
		}
		td[data-label]::before {
			content: attr(data-label) ': ';
			color: var(--text-muted);
			font-weight: 600;
		}
	}
</style>
