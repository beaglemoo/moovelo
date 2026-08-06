<script lang="ts">
	import { page } from '$app/state';
	import { shared, fetchConfig, type SharedRoute } from '$lib/api';
	import { decodePolyline6 } from '$lib/polyline';
	import { cumulativeDistances, pointAtDistance } from '$lib/geo';
	import MapView from '$lib/map/MapView.svelte';
	import ElevationProfile from '$lib/components/ElevationProfile.svelte';

	const token = page.params.token ?? '';

	let route: SharedRoute | null = $state(null);
	let error: string | null = $state(null);
	let cyclosmTileUrl: string | null = $state(null);
	let fitTrigger = $state(0);
	let hoverPoint: [number, number] | null = $state(null);

	fetchConfig().then((config) => (cyclosmTileUrl = config.tile_url_cyclosm));
	shared
		.get(token)
		.then((r) => {
			route = r;
			fitTrigger += 1;
		})
		.catch(() => (error = 'This shared route does not exist (the link may have been revoked).'));

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
	const routeDists = $derived(cumulativeDistances(routeLine));

	function onHover(distM: number | null) {
		hoverPoint = distM === null ? null : pointAtDistance(routeLine, routeDists, distM);
	}

	function km(m: number): string {
		return `${(m / 1000).toFixed(1)} km`;
	}
</script>

<svelte:head>
	<title>{route ? `${route.name} - Moovelo` : 'Shared route - Moovelo'}</title>
</svelte:head>

<div class="share-page">
	<header>
		<span class="brand">Moovelo</span>
		{#if route}
			<span class="name">{route.name}</span>
			<span class="stats">
				{km(route.distance_m)} &middot; {Math.round(route.ascent_m)} m up &middot; {route.preset}
			</span>
			<span class="spacer"></span>
			<a class="download" href={shared.gpxUrl(token)}>Download GPX</a>
		{/if}
	</header>

	{#if error}
		<p class="error">{error}</p>
	{:else}
		<div class="map">
			<MapView
				waypoints={[]}
				{routeLine}
				legStartIndices={[]}
				{hoverPoint}
				{cyclosmTileUrl}
				{fitTrigger}
				onAddWaypoint={() => {}}
				onMoveWaypoint={() => {}}
				onInsertVia={() => {}}
				onRemoveWaypoint={() => {}}
				onSetStart={() => {}}
				onSetEnd={() => {}}
				onClear={() => {}}
			/>
		</div>
		{#if route && route.elevation.length}
			<div class="profile">
				<ElevationProfile elevation={route.elevation} {onHover} />
			</div>
		{/if}
	{/if}
</div>

<style>
	.share-page {
		display: flex;
		flex-direction: column;
		height: 100vh;
		height: 100dvh;
	}
	header {
		display: flex;
		align-items: center;
		gap: 0.8rem;
		padding: 0 0.9rem;
		height: 42px;
		background: #073642;
		color: #eee8d5;
		flex-shrink: 0;
		flex-wrap: nowrap;
		overflow: hidden;
	}
	.brand {
		font-weight: 700;
	}
	.name {
		font-weight: 600;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.stats {
		font-size: 0.85rem;
		color: #93a1a1;
		white-space: nowrap;
	}
	.spacer {
		flex: 1;
	}
	.download {
		color: #eee8d5;
		border: 1px solid #586e75;
		border-radius: 6px;
		padding: 0.25rem 0.7rem;
		font-size: 0.85rem;
		text-decoration: none;
		white-space: nowrap;
	}
	.map {
		flex: 1;
		min-height: 0;
	}
	.profile {
		flex-shrink: 0;
		border-top: 1px solid #eee8d5;
	}
	.error {
		padding: 2rem;
		color: #dc322f;
	}
</style>
