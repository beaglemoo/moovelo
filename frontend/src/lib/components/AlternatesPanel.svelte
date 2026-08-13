<script lang="ts">
	import type { RouteResponse } from '$lib/api';
	import { distanceDelta, elevationDelta } from '$lib/format';
	import { units } from '$lib/units.svelte';

	interface Props {
		/** The route currently on screen - deltas are shown against this, not
		 * against each other. */
		current: RouteResponse;
		loading: boolean;
		error: string | null;
		/** null while a search is in flight or hasn't run; [] when Valhalla
		 * returned none - fewer (or zero) than requested is normal, not a
		 * failure, so an empty list still gets its own message rather than
		 * this panel simply not appearing. */
		alternates: RouteResponse[] | null;
		hoveredIndex: number | null;
		onHover: (index: number | null) => void;
		onUse: (index: number) => void;
		onDismiss: () => void;
	}

	let { current, loading, error, alternates, hoveredIndex, onHover, onUse, onDismiss }: Props =
		$props();

	/** "+3.2 km" / "-1.0 km" - sign-aware, since a shorter alternate is just
	 * as useful a fact as a longer one. */
	function deltaKm(alt: RouteResponse): string {
		return distanceDelta(alt.distance_m - current.distance_m, units.system);
	}

	function deltaAscent(alt: RouteResponse): string {
		return elevationDelta(alt.ascent_m - current.ascent_m, units.system);
	}
</script>

<div class="alternates-panel" role="dialog" aria-label="Alternative routes">
	<div class="header">
		<h3>Alternative routes</h3>
		<button type="button" class="close" onclick={onDismiss} aria-label="Close">×</button>
	</div>
	{#if loading}
		<p class="note">Searching…</p>
	{:else if error}
		<p class="note error">{error}</p>
	{:else if alternates && alternates.length === 0}
		<p class="note">Valhalla found no distinct alternative for this route.</p>
	{:else if alternates}
		<ul class="candidates">
			{#each alternates as alt, i (i)}
				<li
					class:hovered={i === hoveredIndex}
					onmouseenter={() => onHover(i)}
					onmouseleave={() => onHover(null)}
				>
					<span class="dot"></span>
					<span class="stats">Alternative {i + 1} · {deltaKm(alt)} · ↗ {deltaAscent(alt)}</span>
					<button type="button" onclick={() => onUse(i)}>Use</button>
				</li>
			{/each}
		</ul>
	{/if}
</div>

<style>
	.alternates-panel {
		position: absolute;
		top: 52px;
		right: 10px;
		z-index: 8;
		width: min(280px, 85vw);
		background: #fff;
		border: 1px solid #ddd;
		border-radius: 8px;
		box-shadow: 0 8px 30px #0003;
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
		color: #073642;
	}
	.close {
		border: 0;
		background: none;
		font-size: 1.1rem;
		line-height: 1;
		color: #93a1a1;
		cursor: pointer;
		padding: 0 4px;
	}
	.close:hover {
		color: #dc322f;
	}
	.note {
		margin: 0;
		color: #93a1a1;
	}
	.note.error {
		color: #dc322f;
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
		background: #eee8d5;
	}
	.dot {
		flex: none;
		width: 9px;
		height: 9px;
		border-radius: 50%;
		background: #93a1a1;
	}
	.stats {
		flex: 1;
		min-width: 0;
		color: #073642;
	}
	.candidates button {
		flex: none;
		border: 1px solid #ccc;
		background: #fff;
		border-radius: 5px;
		padding: 0.25rem 0.55rem;
		font: inherit;
		font-size: 0.78rem;
		cursor: pointer;
	}
	.candidates button:hover {
		border-color: #268bd2;
		color: #268bd2;
	}
</style>
