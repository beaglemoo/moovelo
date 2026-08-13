<script lang="ts">
	import { PAVED_SURFACES } from '$lib/surface';
	import type { LoopCandidate } from '$lib/api';
	import { distance, elevation, KM_PER_MILE } from '$lib/format';
	import { units } from '$lib/units.svelte';

	interface Props {
		targetKm: number;
		onTargetKmChange: (value: number) => void;
		loading: boolean;
		error: string | null;
		/** null before the first search; [] when a search found nothing. */
		candidates: LoopCandidate[] | null;
		hoveredIndex: number | null;
		onHover: (index: number | null) => void;
		onFind: () => void;
		onUse: (candidate: LoopCandidate) => void;
		onDismiss: () => void;
	}

	let {
		targetKm,
		onTargetKmChange,
		loading,
		error,
		candidates,
		hoveredIndex,
		onHover,
		onFind,
		onUse,
		onDismiss
	}: Props = $props();

	// Matches MapView's loop-preview line-color match expression - the same
	// three candidates, the same three colours.
	const COLOURS = ['#268bd2', '#b58900', '#d33682'];

	// The wire contract (targetKm, onTargetKmChange) stays metric end to end -
	// the backend only ever sees km. This panel is the only place that needs
	// to know about miles, so the km<->mi conversion lives entirely here
	// rather than leaking into +page.svelte's state, matching the toDisplay/
	// fromDisplay pattern ElevationProfile.svelte uses for its own axis.
	const imperial = $derived(units.system === 'imperial');
	// Snapped to the input's own step (5) rather than the nearest integer -
	// the number input enforces step validation on submit against whatever
	// value is showing, touched or not, so an unsnapped default (60 km is
	// 37.28 mi, off the 5-wide grid) would silently block the very first
	// "Find loops" click before the rider even touches the field.
	const targetDisplay = $derived(imperial ? Math.round(targetKm / KM_PER_MILE / 5) * 5 : targetKm);

	function onTargetInput(event: Event & { currentTarget: HTMLInputElement }) {
		const value = Number(event.currentTarget.value);
		onTargetKmChange(imperial ? value * KM_PER_MILE : value);
	}

	function pavedPercent(candidate: LoopCandidate): number | null {
		const surface = candidate.snapshot.surface;
		if (!surface || surface.total_m <= 0) return null;
		const paved = PAVED_SURFACES.reduce((total, key) => total + (surface.surface_m[key] ?? 0), 0);
		return Math.round((paved / surface.total_m) * 100);
	}

	function submit(event: SubmitEvent) {
		event.preventDefault();
		onFind();
	}
</script>

<div class="loop-panel" role="dialog" aria-label="Loop from here">
	<div class="header">
		<h3>Loop from here</h3>
		<button type="button" class="close" onclick={onDismiss} aria-label="Close">×</button>
	</div>
	<form class="target" onsubmit={submit}>
		<label>
			Target distance ({imperial ? 'mi' : 'km'})
			<input
				type="number"
				min="5"
				max={imperial ? 125 : 200}
				step="5"
				value={targetDisplay}
				oninput={onTargetInput}
			/>
		</label>
		<button type="submit" disabled={loading}>{loading ? 'Searching…' : 'Find loops'}</button>
	</form>
	{#if error}
		<p class="note error">{error}</p>
	{/if}
	{#if candidates && candidates.length === 0}
		<p class="note">No rideable loop found from here.</p>
	{/if}
	{#if candidates && candidates.length > 0}
		<ul class="candidates">
			{#each candidates as candidate, i (i)}
				<li
					class:hovered={i === hoveredIndex}
					onmouseenter={() => onHover(i)}
					onmouseleave={() => onHover(null)}
				>
					<span class="dot" style="background: {COLOURS[i % COLOURS.length]}"></span>
					<span class="stats">
						{distance(candidate.snapshot.distance_m, units.system)} · ↗ {elevation(
							candidate.snapshot.ascent_m,
							units.system
						)}
						{#if pavedPercent(candidate) !== null}
							· {pavedPercent(candidate)}% paved
						{/if}
					</span>
					<button type="button" onclick={() => onUse(candidate)}>Use this loop</button>
				</li>
			{/each}
		</ul>
	{/if}
</div>

<style>
	.loop-panel {
		position: absolute;
		top: 52px;
		right: 10px;
		z-index: 8;
		width: min(300px, 85vw);
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		box-shadow: 0 8px 30px var(--shadow);
		padding: 0.7rem 0.8rem;
		display: flex;
		flex-direction: column;
		gap: 0.55rem;
		font-size: 0.85rem;
	}
	.header {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}
	.header h3 {
		margin: 0;
		font-size: 0.95rem;
		color: var(--text);
	}
	.close {
		border: 0;
		background: none;
		font-size: 1.1rem;
		line-height: 1;
		color: var(--text-muted);
		cursor: pointer;
		padding: 0 4px;
	}
	.close:hover {
		color: var(--danger-text);
	}
	.target {
		display: flex;
		align-items: flex-end;
		gap: 0.5rem;
	}
	.target label {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		color: var(--text-muted);
		font-size: 0.78rem;
	}
	.target input {
		width: 5rem;
		font: inherit;
		padding: 0.3rem 0.4rem;
		border: 1px solid var(--input-border);
		background: var(--input-bg);
		color: var(--text);
		border-radius: 5px;
	}
	.target button {
		border: 1px solid var(--accent);
		background: var(--accent);
		color: #fff;
		border-radius: 5px;
		padding: 0.35rem 0.7rem;
		font: inherit;
		font-size: 0.82rem;
		cursor: pointer;
	}
	.target button:disabled {
		opacity: 0.6;
		cursor: default;
	}
	.note {
		margin: 0;
		color: var(--text-muted);
	}
	.note.error {
		color: var(--danger-text);
	}
	.candidates {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 3px;
	}
	.candidates li {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 4px 6px;
		border-radius: 5px;
	}
	.candidates li.hovered {
		background: var(--surface-sunken);
	}
	.dot {
		flex: none;
		width: 9px;
		height: 9px;
		border-radius: 50%;
	}
	.stats {
		flex: 1;
		min-width: 0;
		color: var(--text);
	}
	.candidates button {
		flex: none;
		border: 1px solid var(--input-border);
		background: var(--surface);
		color: var(--text);
		border-radius: 5px;
		padding: 0.25rem 0.55rem;
		font: inherit;
		font-size: 0.78rem;
		cursor: pointer;
	}
	.candidates button:hover {
		border-color: var(--accent);
		color: var(--link);
	}
</style>
