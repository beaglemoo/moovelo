<script lang="ts">
	import {
		customPresets as customPresetsApi,
		type BicycleCostingOptions,
		type CustomPreset,
		type Preset
	} from '$lib/api';
	import { untrack } from 'svelte';

	interface Props {
		/** The last selected named preset - seeds the sliders when opened
		 * without an already-active custom bundle. */
		preset: Preset;
		/** The currently active custom bundle, if any - seeds the sliders
		 * instead of `preset` when set. */
		current: BicycleCostingOptions | null;
		onApply: (options: BicycleCostingOptions) => void;
		onClose: () => void;
	}

	let { preset, current, onApply, onClose }: Props = $props();

	// Mirrors services/presets.py's PRESETS. The sliders need instant
	// defaults before any network round trip, so these are duplicated
	// client-side rather than fetched - keep in sync if the backend bundles
	// ever change.
	const PRESET_DICTS: Record<Preset, BicycleCostingOptions> = {
		road: {
			bicycle_type: 'Road',
			cycling_speed: 25,
			use_roads: 0.6,
			use_hills: 0.5,
			avoid_bad_surfaces: 0.9
		},
		gravel: {
			bicycle_type: 'Cross',
			cycling_speed: 20,
			use_roads: 0.3,
			use_hills: 0.5,
			avoid_bad_surfaces: 0.0
		},
		quiet: {
			bicycle_type: 'Hybrid',
			cycling_speed: 18,
			use_roads: 0.1,
			use_hills: 0.3,
			avoid_bad_surfaces: 0.4
		}
	};

	// Seeds the sliders once, on mount - this popover is created fresh each
	// time it opens (see the {#if} in +page.svelte), so there is nothing to
	// react to afterwards, and `untrack` makes that one-time read explicit.
	const seed = untrack(() => current ?? PRESET_DICTS[preset]);

	let bicycleType = $state(seed.bicycle_type);
	let cyclingSpeed = $state(seed.cycling_speed);
	let useRoads = $state(seed.use_roads);
	let useHills = $state(seed.use_hills);
	let avoidBadSurfaces = $state(seed.avoid_bad_surfaces);

	const options = $derived<BicycleCostingOptions>({
		bicycle_type: bicycleType,
		cycling_speed: cyclingSpeed,
		use_roads: useRoads,
		use_hills: useHills,
		avoid_bad_surfaces: avoidBadSurfaces
	});

	// Fires on release/blur rather than on every drag tick (`onchange`, not
	// `oninput`) - a slider dragged across its range would otherwise trigger
	// a reroute per pixel.
	function apply() {
		onApply(options);
	}

	let saved: CustomPreset[] = $state([]);
	let loadError = $state(false);
	customPresetsApi
		.list()
		.then((list) => (saved = list))
		.catch(() => (loadError = true));

	// The saved preset the sliders were last set from, so a further tweak
	// can be flagged as diverging from it - the same " *" convention the
	// toolbar's save button uses for an unsaved route.
	let appliedPresetId: string | null = $state(null);
	const appliedPreset = $derived(saved.find((p) => p.id === appliedPresetId) ?? null);
	const modified = $derived(
		appliedPreset !== null &&
			(appliedPreset.options.bicycle_type !== bicycleType ||
				appliedPreset.options.cycling_speed !== cyclingSpeed ||
				appliedPreset.options.use_roads !== useRoads ||
				appliedPreset.options.use_hills !== useHills ||
				appliedPreset.options.avoid_bad_surfaces !== avoidBadSurfaces)
	);

	function applyPreset(p: CustomPreset) {
		bicycleType = p.options.bicycle_type;
		cyclingSpeed = p.options.cycling_speed;
		useRoads = p.options.use_roads;
		useHills = p.options.use_hills;
		avoidBadSurfaces = p.options.avoid_bad_surfaces;
		appliedPresetId = p.id;
		onApply(p.options);
	}

	async function removePreset(p: CustomPreset) {
		await customPresetsApi.remove(p.id);
		saved = saved.filter((s) => s.id !== p.id);
		if (appliedPresetId === p.id) appliedPresetId = null;
	}

	let nameInput = $state('');
	let saving = $state(false);
	let saveError: string | null = $state(null);

	async function saveAsPreset() {
		const name = nameInput.trim();
		if (!name) return;
		saving = true;
		saveError = null;
		try {
			const createdPreset = await customPresetsApi.create(name, options);
			saved = [...saved, createdPreset].sort((a, b) => a.name.localeCompare(b.name));
			appliedPresetId = createdPreset.id;
			nameInput = '';
		} catch (err) {
			saveError = err instanceof Error ? err.message : 'Could not save preset';
		} finally {
			saving = false;
		}
	}

	function onKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape') onClose();
	}
</script>

<div
	class="popover"
	role="dialog"
	aria-label="Custom costing options"
	tabindex="-1"
	onkeydown={onKeydown}
>
	<div class="sliders">
		<label>
			Bike type
			<select bind:value={bicycleType} onchange={apply}>
				<option value="Road">Road</option>
				<option value="Hybrid">Hybrid</option>
				<option value="Cross">Cross</option>
				<option value="Mountain">Mountain</option>
			</select>
		</label>
		<label>
			<!-- Kept in km/h even in imperial mode: this is the raw costing knob,
			     and its scale/steps are km/h. Converting only the readout made a
			     one-notch drag often show the same rounded mph, reading as an
			     unresponsive slider - the same reason the /settings speed input
			     stays metric. -->
			Cycling speed: {cyclingSpeed} km/h
			<input
				type="range"
				min="10"
				max="35"
				step="1"
				bind:value={cyclingSpeed}
				onchange={apply}
				aria-label="Cycling speed"
			/>
		</label>
		<label>
			Prefer roads: {Math.round(useRoads * 100)}%
			<input
				type="range"
				min="0"
				max="1"
				step="0.05"
				bind:value={useRoads}
				onchange={apply}
				aria-label="Prefer roads"
			/>
		</label>
		<label>
			Hills OK: {Math.round(useHills * 100)}%
			<input
				type="range"
				min="0"
				max="1"
				step="0.05"
				bind:value={useHills}
				onchange={apply}
				aria-label="Hills OK"
			/>
		</label>
		<label>
			Avoid rough surfaces: {Math.round(avoidBadSurfaces * 100)}%
			<input
				type="range"
				min="0"
				max="1"
				step="0.05"
				bind:value={avoidBadSurfaces}
				onchange={apply}
				aria-label="Avoid rough surfaces"
			/>
		</label>
	</div>

	<div class="presets-list">
		<h4>Your presets{modified ? ' *' : ''}</h4>
		{#if loadError}
			<p class="note">Could not load your saved presets.</p>
		{:else if saved.length === 0}
			<p class="note">No saved presets yet.</p>
		{:else}
			<ul>
				{#each saved as p (p.id)}
					<li class:active={p.id === appliedPresetId}>
						<button type="button" class="name" onclick={() => applyPreset(p)}>
							{p.name}{p.id === appliedPresetId && modified ? ' *' : ''}
						</button>
						<button
							type="button"
							class="remove"
							onclick={() => removePreset(p)}
							aria-label={`Delete preset ${p.name}`}
						>
							×
						</button>
					</li>
				{/each}
			</ul>
		{/if}
	</div>

	<form
		class="save-as"
		onsubmit={(event) => {
			event.preventDefault();
			void saveAsPreset();
		}}
	>
		<input
			bind:value={nameInput}
			maxlength="60"
			placeholder="Save as preset…"
			aria-label="New preset name"
		/>
		<button type="submit" disabled={saving || !nameInput.trim()}>Save as preset</button>
	</form>
	{#if saveError}
		<p class="note error">{saveError}</p>
	{/if}

	<button type="button" class="close" onclick={onClose}>Close</button>
</div>

<!-- pointerdown, not click: Svelte flushes this popover into the DOM
     synchronously during the opening button's click handler, so a click
     listener here would catch that same click still bubbling to window and
     close the popover the instant it opened. The opening interaction's
     pointerdown fired before mount, so only the next outside press closes. -->
<svelte:window
	onpointerdown={(event) => {
		if (!(event.target as HTMLElement)?.closest('.popover')) onClose();
	}}
/>

<style>
	.popover {
		position: absolute;
		top: calc(100% + 6px);
		left: 0;
		z-index: 10;
		width: min(300px, 85vw);
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		box-shadow: 0 8px 30px var(--shadow);
		padding: 0.8rem;
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
		font-size: 0.85rem;
	}
	.sliders {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}
	.sliders label {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		color: var(--text);
	}
	.sliders select,
	.sliders input[type='range'] {
		font: inherit;
	}
	.presets-list h4 {
		margin: 0 0 0.3rem;
		font-size: 0.8rem;
		color: var(--text-muted);
	}
	.presets-list ul {
		list-style: none;
		margin: 0;
		padding: 0;
		max-height: 140px;
		overflow-y: auto;
	}
	.presets-list li {
		display: flex;
		align-items: center;
		gap: 4px;
	}
	.presets-list li.active .name {
		/* Was route-brand magenta (#d33682), but as text on the dark popover
		   surface that is 2.86:1; --link clears AA in both themes and still reads
		   as the selected/active accent. Bold keeps it distinct. */
		color: var(--link);
		font-weight: 600;
	}
	.presets-list .name {
		flex: 1;
		text-align: left;
		background: none;
		border: 0;
		padding: 3px 4px;
		font: inherit;
		color: var(--text);
		cursor: pointer;
	}
	.presets-list .name:hover {
		text-decoration: underline;
	}
	.presets-list .remove {
		background: none;
		border: 0;
		color: var(--text-muted);
		cursor: pointer;
		font-size: 1rem;
		line-height: 1;
		padding: 2px 6px;
	}
	.presets-list .remove:hover {
		color: var(--danger-text);
	}
	.note {
		margin: 0;
		color: var(--text-muted);
		font-size: 0.8rem;
	}
	.note.error {
		color: var(--danger-text);
	}
	.save-as {
		display: flex;
		gap: 6px;
	}
	.save-as input {
		flex: 1;
		font: inherit;
		font-size: 0.8rem;
		padding: 4px 6px;
		border: 1px solid var(--input-border);
		border-radius: 4px;
		background: var(--input-bg);
		color: var(--text);
	}
	.save-as button {
		border: 1px solid var(--accent);
		background: var(--accent-fill);
		color: #fff;
		border-radius: 4px;
		padding: 4px 8px;
		font: inherit;
		font-size: 0.8rem;
		cursor: pointer;
	}
	.save-as button:disabled {
		opacity: 0.6;
		cursor: default;
	}
	.close {
		align-self: flex-end;
		border: 1px solid var(--input-border);
		background: var(--surface);
		color: var(--text);
		border-radius: 4px;
		padding: 3px 10px;
		font: inherit;
		font-size: 0.8rem;
		cursor: pointer;
	}
</style>
