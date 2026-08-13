<script lang="ts">
	import type { Climb } from '$lib/api';
	import { categoryColour, categoryLabel } from '$lib/climbs';
	import { distance, elevation, shortLength } from '$lib/format';
	import { units } from '$lib/units.svelte';

	interface Props {
		climbs: Climb[];
		hoveredIndex: number | null;
		onHover: (index: number | null) => void;
	}

	let { climbs, hoveredIndex, onHover }: Props = $props();
</script>

<div class="climbs">
	<h3>Climbs</h3>
	<!-- Fixed height whatever the state, matching PoiPanel's .results: the
	     panel below the map must not grow under the rider's pointer. -->
	<div class="results">
		<ul>
			{#each climbs as climb, i (i)}
				<li>
					<button
						type="button"
						class:hovered={i === hoveredIndex}
						onmouseenter={() => onHover(i)}
						onmouseleave={() => onHover(null)}
						onfocus={() => onHover(i)}
						onblur={() => onHover(null)}
					>
						<span
							class="badge"
							style="background: {categoryColour(climb.category)}"
							title="{distance(climb.length_m, units.system)} at {climb.avg_grade_pct.toFixed(1)}%"
						>
							{categoryLabel(climb.category)}
						</span>
						<span class="at">{shortLength(climb.start_dist_m, units.system)}</span>
						<span class="what">
							{distance(climb.length_m, units.system)} at {climb.avg_grade_pct.toFixed(1)}%, {elevation(
								climb.gain_m,
								units.system
							)}
						</span>
					</button>
				</li>
			{/each}
		</ul>
	</div>
</div>

<style>
	.climbs {
		display: flex;
		flex-direction: column;
		min-height: 0;
	}
	h3 {
		margin: 0;
		padding: 0.2rem 0.2rem 0.3rem;
		font-size: 0.85rem;
		color: var(--text-muted);
	}
	.results {
		height: 150px;
		overflow-y: auto;
	}
	ul {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	li button {
		display: flex;
		align-items: baseline;
		gap: 8px;
		width: 100%;
		border: 0;
		background: none;
		font: inherit;
		font-size: 0.82rem;
		text-align: left;
		padding: 3px 6px;
		border-radius: 3px;
		cursor: default;
	}
	li button.hovered {
		background: var(--surface-sunken);
	}
	.badge {
		flex: none;
		min-width: 1.8em;
		text-align: center;
		border-radius: 3px;
		padding: 1px 5px;
		font-size: 0.72rem;
		font-weight: 600;
		color: #fff;
	}
	.at {
		color: var(--text-muted);
		font-variant-numeric: tabular-nums;
		flex: none;
		width: 3.6em;
	}
	.what {
		color: var(--text);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
</style>
