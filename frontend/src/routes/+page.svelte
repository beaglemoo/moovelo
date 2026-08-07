<script lang="ts">
	import { page } from '$app/state';
	import {
		fetchConfig,
		planRoute,
		routes,
		wahoo,
		type Preset,
		type RouteResponse,
		type RouteSource,
		type WahooStatus,
		type Waypoint
	} from '$lib/api';
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

	let fitTrigger = $state(0);
	let savedId: string | null = $state(null);
	let savedName: string | null = $state(null);
	let dirty = $state(false);
	let saveDialogOpen = $state(false);
	let saveNameInput = $state('');
	let saving = $state(false);
	let wahooStatus: WahooStatus | null = $state(null);
	let wahooPush: 'idle' | 'working' | 'synced' | 'error' = $state('idle');
	let wahooPushError: string | null = $state(null);

	let abortController: AbortController | null = null;

	fetchConfig().then((c) => (config = c));
	wahoo.status().then((s) => (wahooStatus = s));

	async function sendToWahoo() {
		if (!savedId) return;
		wahooPush = 'working';
		wahooPushError = null;
		try {
			await wahoo.push(savedId);
			await pollWahoo(savedId);
		} catch (err) {
			wahooPush = 'error';
			wahooPushError = err instanceof Error ? err.message : 'Push failed';
		}
	}

	async function pollWahoo(id: string) {
		for (let i = 0; i < 40; i++) {
			await new Promise((resolve) => setTimeout(resolve, 3000));
			if (savedId !== id) return;
			const saved = await routes.get(id);
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
		wahooPush = 'error';
		wahooPushError = 'Push is taking unusually long - check the library later';
	}

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

	// Open a saved route when arriving via /?route=<id>.
	const routeParam = page.url.searchParams.get('route');
	if (routeParam) {
		routes
			.get(routeParam)
			.then((saved) => {
				waypoints = saved.waypoints;
				preset = saved.preset;
				source = saved.source;
				route = saved;
				savedId = saved.id;
				savedName = saved.name;
				dirty = false;
				fitTrigger += 1;
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
			dirty = true;
		} catch (err) {
			if (err instanceof DOMException && err.name === 'AbortError') return;
			loading = false;
			error = err instanceof Error ? err.message : 'Routing failed';
		}
	}

	function addWaypoint(wp: Waypoint) {
		if (!mayEdit()) return;
		waypoints.push(wp);
		reroute();
	}

	function moveWaypoint(index: number, wp: Waypoint) {
		if (!mayEdit()) return;
		waypoints[index] = wp;
		reroute();
	}

	function insertVia(position: number, wp: Waypoint) {
		if (!mayEdit()) return;
		waypoints.splice(position, 0, wp);
		reroute();
	}

	function removeWaypoint(index: number) {
		if (!mayEdit()) return;
		waypoints.splice(index, 1);
		reroute();
	}

	function setStart(wp: Waypoint) {
		if (!mayEdit()) return;
		if (waypoints.length === 0) waypoints.push(wp);
		else waypoints[0] = wp;
		reroute();
	}

	function setEnd(wp: Waypoint) {
		if (!mayEdit()) return;
		if (waypoints.length < 2) waypoints.push(wp);
		else waypoints[waypoints.length - 1] = wp;
		reroute();
	}

	function undo() {
		if (!mayEdit()) return;
		waypoints.pop();
		reroute();
	}

	function clear() {
		source = 'planned';
		waypoints = [];
		route = null;
		error = null;
		savedId = null;
		savedName = null;
		dirty = false;
	}

	function defaultName(): string {
		return `Ride ${new Date().toLocaleDateString(undefined, { day: 'numeric', month: 'short' })}`;
	}

	async function save() {
		if (!route || waypoints.length < 2) return;
		if (savedId === null) {
			saveNameInput = savedName ?? defaultName();
			saveDialogOpen = true;
			return;
		}
		saving = true;
		try {
			await routes.update(savedId, { waypoints, preset, snapshot: route });
			dirty = false;
		} catch (err) {
			error = err instanceof Error ? err.message : 'Save failed';
		} finally {
			saving = false;
		}
	}

	async function confirmSave(event: SubmitEvent) {
		event.preventDefault();
		if (!route || !saveNameInput.trim()) return;
		saving = true;
		try {
			const saved = await routes.create({
				name: saveNameInput.trim(),
				waypoints,
				preset,
				snapshot: route
			});
			savedId = saved.id;
			savedName = saved.name;
			dirty = false;
			saveDialogOpen = false;
		} catch (err) {
			error = err instanceof Error ? err.message : 'Save failed';
		} finally {
			saving = false;
		}
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
				{fitTrigger}
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
			<button
				type="button"
				class="save"
				onclick={save}
				disabled={!route || saving || (savedId !== null && !dirty)}
			>
				{savedId === null ? 'Save' : dirty ? 'Save changes' : 'Saved'}
			</button>
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
		</div>
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
				<span>{formatDuration(route.duration_s)}</span>
				<span>↗ {Math.round(route.ascent_m)} m</span>
				<span>↘ {Math.round(route.descent_m)} m</span>
			</div>
			<ElevationProfile elevation={route.elevation} onHover={handleElevationHover} />
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
