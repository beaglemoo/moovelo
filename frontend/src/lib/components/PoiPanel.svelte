<script lang="ts">
	import type { PoiResult } from '$lib/api';
	import { categoryColour, categoryLabel, POI_GROUPS } from '$lib/pois';

	interface Props {
		pois: PoiResult[];
		/** True when the backend cut the list short. Said out loud rather
		 * than presenting a partial answer as a complete one. */
		truncated: boolean;
		loading: boolean;
		selected: string[];
		hoveredId: number | null;
		onToggleGroup: (key: string) => void;
		onHover: (id: number | null) => void;
	}

	let { pois, truncated, loading, selected, hoveredId, onToggleGroup, onHover }: Props = $props();

	function km(metres: number): string {
		return `${(metres / 1000).toFixed(1)} km`;
	}

	function off(metres: number): string {
		return metres < 1000 ? `${Math.round(metres)} m off` : `${(metres / 1000).toFixed(1)} km off`;
	}
</script>

<div class="pois">
	<div class="chips" role="group" aria-label="Points of interest">
		{#each POI_GROUPS as group (group.key)}
			<button
				type="button"
				class="chip"
				class:on={selected.includes(group.key)}
				aria-pressed={selected.includes(group.key)}
				style="--chip: {group.colour}"
				onclick={() => onToggleGroup(group.key)}
			>
				{group.label}
			</button>
		{/each}
	</div>

	<!-- Fixed height whatever the state. Otherwise the panel grows when
	     results arrive, the map above it resizes, and everything the rider
	     was pointing at moves - which is how a context menu ended up
	     closing itself under them. -->
	<div class="results">
		{#if selected.length === 0}
			<p class="note">Pick a category to see what is on this ride.</p>
		{:else if loading && pois.length === 0}
			<p class="note">Looking…</p>
		{:else if pois.length === 0}
			<p class="note">Nothing of those kinds near this route.</p>
		{:else}
			<ul>
				{#each pois as poi (poi.id)}
					<li>
						<button
							type="button"
							class:hovered={poi.id === hoveredId}
							onmouseenter={() => onHover(poi.id)}
							onmouseleave={() => onHover(null)}
							onfocus={() => onHover(poi.id)}
							onblur={() => onHover(null)}
						>
							<span class="dot" style="background: {categoryColour(poi.category)}"></span>
							<span class="at">{km(poi.dist_along_m)}</span>
							<!-- OSM strings are untrusted input: text only, never markup. -->
							<span class="what">{poi.name ?? categoryLabel(poi.category)}</span>
							<span class="meta">
								{poi.name ? categoryLabel(poi.category) : ''}
								{#if poi.tags.opening_hours}
									<span class="hours" title={poi.tags.opening_hours}>{poi.tags.opening_hours}</span>
								{/if}
							</span>
							<span class="off">{off(poi.dist_from_route_m)}</span>
						</button>
					</li>
				{/each}
			</ul>
			{#if truncated}
				<p class="note">
					Showing {pois.length} of what is near this ride. Narrow the categories to see more.
				</p>
			{/if}
		{/if}
	</div>
</div>

<style>
	.pois {
		display: flex;
		flex-direction: column;
		min-height: 0;
	}
	.chips {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
		padding: 0.2rem 0.2rem 0.4rem;
	}
	.chip {
		border: 1px solid #ddd6c1;
		background: #fff;
		color: #586e75;
		border-radius: 999px;
		padding: 2px 10px;
		font: inherit;
		font-size: 0.78rem;
		cursor: pointer;
	}
	.chip:hover {
		border-color: var(--chip);
	}
	.chip.on {
		background: var(--chip);
		border-color: var(--chip);
		color: #fff;
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
		background: #eee8d5;
	}
	.dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		flex: none;
		align-self: center;
	}
	.at {
		color: #586e75;
		font-variant-numeric: tabular-nums;
		flex: none;
		width: 4.2em;
	}
	.what {
		color: #073642;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.meta {
		color: #93a1a1;
		font-size: 0.75rem;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		flex: 1;
	}
	.hours {
		margin-left: 6px;
	}
	.off {
		color: #93a1a1;
		font-size: 0.75rem;
		flex: none;
	}
	.note {
		margin: 0;
		padding: 4px 6px 6px;
		color: #93a1a1;
		font-size: 0.8rem;
	}
</style>
