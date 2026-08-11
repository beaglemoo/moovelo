<script lang="ts">
	import { cueCount, type ImportResult } from '$lib/import.svelte';
	import type { ActivityDetail, SavedRoute } from '$lib/api';
	import { km } from '$lib/format';

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
							{km(result.item.distance_m)} ·
							{Math.round(result.item.ascent_m)} m ascent
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
		border: 1px solid #d5d5d5;
		border-radius: 6px;
		padding: 0.6rem 0.8rem;
		margin-bottom: 1rem;
		background: #fafafa;
	}
	.results-head {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: 0.4rem;
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
		color: #555;
	}
	.error {
		color: #b00020;
	}
	.warn {
		color: #a05a00;
	}
	li.failed .file {
		color: #b00020;
	}
</style>
