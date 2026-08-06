<script lang="ts">
	import { fetchConfig, planRoute, type Preset, type RouteResponse, type Waypoint } from '$lib/api';
	import { cumulativeDistances, pointAtDistance } from '$lib/geo';
	import { decodePolyline6 } from '$lib/polyline';
	import ElevationProfile from '$lib/components/ElevationProfile.svelte';
	import PresetSelector from '$lib/components/PresetSelector.svelte';
	import MapView from '$lib/map/MapView.svelte';

	let waypoints: Waypoint[] = $state([]);
	let preset: Preset = $state('road');
	let route: RouteResponse | null = $state(null);
	let loading = $state(false);
	let error: string | null = $state(null);
	let hoverPoint: [number, number] | null = $state(null);
	let config: { tile_url_cyclosm: string | null } | null = $state(null);

	let abortController: AbortController | null = null;

	fetchConfig().then((c) => (config = c));

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
			route = await planRoute(waypoints, preset, abortController.signal);
			loading = false;
		} catch (err) {
			if (err instanceof DOMException && err.name === 'AbortError') return;
			loading = false;
			error = err instanceof Error ? err.message : 'Routing failed';
		}
	}

	function addWaypoint(wp: Waypoint) {
		waypoints.push(wp);
		reroute();
	}

	function moveWaypoint(index: number, wp: Waypoint) {
		waypoints[index] = wp;
		reroute();
	}

	function insertVia(position: number, wp: Waypoint) {
		waypoints.splice(position, 0, wp);
		reroute();
	}

	function removeWaypoint(index: number) {
		waypoints.splice(index, 1);
		reroute();
	}

	function setStart(wp: Waypoint) {
		if (waypoints.length === 0) waypoints.push(wp);
		else waypoints[0] = wp;
		reroute();
	}

	function setEnd(wp: Waypoint) {
		if (waypoints.length < 2) waypoints.push(wp);
		else waypoints[waypoints.length - 1] = wp;
		reroute();
	}

	function undo() {
		waypoints.pop();
		reroute();
	}

	function clear() {
		waypoints = [];
		route = null;
		error = null;
	}

	function changePreset(next: Preset) {
		preset = next;
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

<div class="app">
	<div class="map-area">
		{#if config}
			<MapView
				{waypoints}
				{routeLine}
				{legStartIndices}
				{hoverPoint}
				cyclosmTileUrl={config.tile_url_cyclosm}
				onAddWaypoint={addWaypoint}
				onMoveWaypoint={moveWaypoint}
				onInsertVia={insertVia}
				onRemoveWaypoint={removeWaypoint}
				onSetStart={setStart}
				onSetEnd={setEnd}
				onClear={clear}
			/>
		{/if}
		<div class="toolbar">
			<PresetSelector {preset} onChange={changePreset} />
			<button type="button" onclick={undo} disabled={waypoints.length === 0}>Undo</button>
			<button type="button" onclick={clear} disabled={waypoints.length === 0}>Clear</button>
		</div>
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
				<span>{formatDuration(route.duration_s)}</span>
				<span>↗ {Math.round(route.ascent_m)} m</span>
				<span>↘ {Math.round(route.descent_m)} m</span>
			</div>
			<ElevationProfile elevation={route.elevation} onHover={handleElevationHover} />
		</div>
	{/if}
</div>

<style>
	:global(html, body) {
		margin: 0;
		height: 100%;
	}
	.app {
		display: flex;
		flex-direction: column;
		height: 100vh;
		height: 100dvh;
		font-family:
			system-ui,
			-apple-system,
			sans-serif;
	}
	.map-area {
		position: relative;
		flex: 1;
		min-height: 0;
	}
	.toolbar {
		position: absolute;
		top: 10px;
		left: 10px;
		display: flex;
		gap: 8px;
		align-items: center;
		z-index: 5;
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
</style>
