<script lang="ts">
	import { cueCount, importQueue } from '$lib/import.svelte';

	const results = $derived(importQueue.results);
</script>

{#if results.length > 0}
	<div class="results">
		<div class="results-head">
			<strong>Import</strong>
			{#if !importQueue.busy}
				<button type="button" onclick={() => importQueue.clear()}>Dismiss</button>
			{/if}
		</div>
		<ul>
			{#each results as result (result.filename + result.status)}
				<li class={result.status}>
					<span class="file">{result.filename}</span>
					{#if result.status === 'waiting'}
						<span class="detail">waiting</span>
					{:else if result.status === 'importing'}
						<span class="detail">importing…</span>
					{:else if result.status === 'failed'}
						<span class="detail error">{result.error}</span>
					{:else if result.route}
						{@const cues = cueCount(result.route)}
						<span class="detail">
							{(result.route.distance_m / 1000).toFixed(1)} km ·
							{Math.round(result.route.ascent_m)} m ascent ·
							{#if cues > 0}
								{cues} turn cues
							{:else}
								<span class="warn">no turn cues - could not match to roads</span>
							{/if}
						</span>
						<a href={`/?route=${result.route.id}`}>View</a>
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
