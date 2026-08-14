<script lang="ts">
	import { activities, type ClimbLogEntry } from '$lib/api';
	import { categoryColour, categoryLabel } from '$lib/climbs';
	import { readableText } from '$lib/contrast';
	import { distance, elevation } from '$lib/format';
	import { units } from '$lib/units.svelte';

	// Same shape as CoverageCard: fetch once on mount, show nothing at all
	// (not an empty card) until there is something to say, and keep the
	// error message the same one the coverage card's own sections use.
	let entries: ClimbLogEntry[] = $state([]);
	let error: string | null = $state(null);
	let loaded = $state(false);

	async function load() {
		try {
			entries = (await activities.climbLog()).climbs;
		} catch (err) {
			error = err instanceof Error ? err.message : 'Could not load climbs';
		} finally {
			loaded = true;
		}
	}
	load();

	const showCard = $derived(loaded && (entries.length > 0 || error !== null));
</script>

{#if showCard}
	<div class="card">
		<h4>Climbs</h4>
		{#if error}
			<p class="error">{error}</p>
		{:else}
			<ul>
				{#each entries as entry, i (i)}
					<li>
						<span
							class="badge"
							style="background: {categoryColour(entry.category)}; color: {readableText(
								categoryColour(entry.category)
							)}"
						>
							{categoryLabel(entry.category)}
						</span>
						<span class="what">
							{distance(entry.length_m, units.system)} at {entry.avg_grade_pct.toFixed(1)}%, {elevation(
								entry.gain_m,
								units.system
							)}
						</span>
						<span class="count">
							{entry.ascent_count}
							{entry.ascent_count === 1 ? 'ascent' : 'ascents'}
						</span>
					</li>
				{/each}
			</ul>
		{/if}
	</div>
{/if}

<style>
	.card {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 0.7rem 0.9rem;
		margin: 0.8rem 0;
		font-size: 0.9rem;
	}
	h4 {
		margin: 0 0 0.4rem;
		font-size: 0.75rem;
		font-weight: 600;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}
	ul {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
	}
	li {
		display: flex;
		align-items: baseline;
		gap: 0.5rem;
		flex-wrap: wrap;
	}
	.badge {
		flex: none;
		min-width: 1.8em;
		text-align: center;
		border-radius: 3px;
		padding: 1px 5px;
		font-size: 0.72rem;
		font-weight: 600;
	}
	.what {
		color: var(--text);
	}
	.count {
		color: var(--text-muted);
		margin-left: auto;
	}
	.error {
		color: var(--danger-text);
		margin: 0;
	}
</style>
