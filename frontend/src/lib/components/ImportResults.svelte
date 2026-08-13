<script lang="ts">
	import { cueCount, type ImportResult } from '$lib/import.svelte';
	import type { ActivityDetail, SavedRoute } from '$lib/api';
	import { distance, elevation } from '$lib/format';
	import { units } from '$lib/units.svelte';

	/** Structural rather than the class itself, so this renders for either
	 * queue without importing both and without a union of two generics. */
	interface Queue {
		results: ImportResult<SavedRoute | ActivityDetail>[];
		busy: boolean;
		clear(): void;
	}

	const { queue }: { queue: Queue } = $props();

	const results = $derived(queue.results);

	/** A route carries legs; an activity never does. */
	function asRoute(item: SavedRoute | ActivityDetail): SavedRoute | null {
		return 'legs' in item ? item : null;
	}
</script>

{#if results.length > 0}
	<div class="results">
		<div class="results-head">
			<strong>Import</strong>
			{#if !queue.busy}
				<button type="button" onclick={() => queue.clear()}>Dismiss</button>
			{/if}
		</div>
		<ul>
			{#each results as result (result.id)}
				<li class={result.status}>
					<span class="file">{result.filename}</span>
					{#if result.status === 'waiting'}
						<span class="detail">waiting</span>
					{:else if result.status === 'importing'}
						<span class="detail">importing…</span>
					{:else if result.status === 'failed'}
						<span class="detail error">{result.error}</span>
					{:else if result.item}
						{@const route = asRoute(result.item)}
						<span class="detail">
							{distance(result.item.distance_m, units.system)} ·
							{elevation(result.item.ascent_m, units.system)} ascent
							{#if route}
								{@const cues = cueCount(route)}
								·
								{#if cues > 0}
									{cues} turn cues
								{:else}
									<span class="warn">no turn cues - could not match to roads</span>
								{/if}
							{/if}
						</span>
						{#if route}
							<a href={`/?route=${route.id}`}>View</a>
						{/if}
					{/if}
				</li>
			{/each}
		</ul>
	</div>
{/if}

<style>
	.results {
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 0.6rem 0.8rem;
		margin-bottom: 1rem;
		background: var(--surface);
	}
	.results-head {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: 0.4rem;
	}
	.results-head button {
		font: inherit;
		font-size: 0.8rem;
		padding: 0.2rem 0.6rem;
		border: 1px solid var(--input-border);
		border-radius: 6px;
		background: var(--surface);
		color: var(--text);
		cursor: pointer;
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
		flex-wrap: wrap;
		gap: 0.5rem;
		align-items: baseline;
		font-size: 0.9rem;
	}
	.file {
		font-weight: 600;
	}
	.detail {
		color: var(--text-muted);
	}
	.error {
		color: var(--danger);
	}
	.warn {
		color: var(--warning);
	}
	li.failed .file {
		color: var(--danger);
	}
</style>
