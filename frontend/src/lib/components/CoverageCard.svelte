<script lang="ts">
	import {
		coverage,
		type CoverageBackfillStatus,
		type CoverageResponse,
		type NetworkCoverage
	} from '$lib/api';
	import { onDestroy } from 'svelte';

	// icn/ncn/rcn/lcn, in the order a rider would expect to read them - widest
	// network first - and only shown when the bbox actually has one.
	const NETWORK_LABELS: Record<string, string> = {
		icn: 'International cycle routes',
		ncn: 'National Cycle Network',
		rcn: 'Regional cycle routes',
		lcn: 'Local cycle routes'
	};
	const NETWORK_ORDER = ['icn', 'ncn', 'rcn', 'lcn'];

	let data: CoverageResponse | null = $state(null);
	let loadError: string | null = $state(null);
	let job: CoverageBackfillStatus | null = $state(null);
	let jobError: string | null = $state(null);
	let pollTimer: ReturnType<typeof setTimeout> | null = null;

	onDestroy(() => {
		if (pollTimer) clearTimeout(pollTimer);
	});

	async function load() {
		try {
			data = await coverage.cycleNetwork();
		} catch (err) {
			loadError = err instanceof Error ? err.message : 'Could not load coverage';
		}
	}
	load();

	async function runBackfill() {
		jobError = null;
		try {
			job = await coverage.backfill();
			pollJob();
		} catch (err) {
			jobError = err instanceof Error ? err.message : 'Could not start matching';
		}
	}

	function pollJob() {
		if (pollTimer) clearTimeout(pollTimer);
		pollTimer = setTimeout(async () => {
			if (!job) return;
			try {
				job = await coverage.backfillStatus(job.id);
			} catch {
				job = null;
				return;
			}
			if (job.status === 'queued' || job.status === 'running') {
				pollJob();
			} else {
				// A finished backfill is exactly what changed the numbers below.
				void load();
			}
		}, 1500);
	}

	function sortedRows(current: CoverageResponse | null): NetworkCoverage[] {
		if (!current) return [];
		return current.networks
			.filter((row) => row.total_m > 0)
			.sort((a, b) => NETWORK_ORDER.indexOf(a.network) - NETWORK_ORDER.indexOf(b.network));
	}
	const rows: NetworkCoverage[] = $derived(sortedRows(data));
</script>

{#if loadError}
	<p class="error">{loadError}</p>
{:else if data && !data.available}
	<div class="card muted">
		<p>{data.reason}</p>
	</div>
{:else if data && rows.length > 0}
	<div class="card">
		<div class="rows">
			{#each rows as row (row.network)}
				<div class="row">
					<span class="label">{NETWORK_LABELS[row.network] ?? row.network}</span>
					<span class="pct">{Math.round((row.ridden_m / row.total_m) * 100)}%</span>
				</div>
			{/each}
		</div>
		<button type="button" onclick={runBackfill} disabled={job?.status === 'running'}>
			{job?.status === 'running' || job?.status === 'queued'
				? `Matching… ${job.matched + job.unmatched} of ${job.total || '?'}`
				: 'Match older rides'}
		</button>
		{#if jobError}
			<p class="error">{jobError}</p>
		{/if}
	</div>
{/if}

<style>
	.card {
		background: #fdf6e3;
		border: 1px solid #eee8d5;
		border-radius: 6px;
		padding: 0.7rem 0.9rem;
		margin: 0.8rem 0;
		font-size: 0.9rem;
	}
	.card.muted {
		color: #657b83;
	}
	.rows {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		margin-bottom: 0.5rem;
	}
	.row {
		display: flex;
		justify-content: space-between;
		gap: 1rem;
	}
	.label {
		color: #073642;
	}
	.pct {
		font-weight: 600;
		color: #268bd2;
	}
	.error {
		color: #dc322f;
	}
	button {
		font-size: 0.85rem;
		padding: 0.3rem 0.7rem;
	}
</style>
