<script lang="ts">
	import {
		ApiError,
		coverage,
		type CoverageBackfillStatus,
		type CoverageResponse,
		type HighwayCoverage,
		type NetworkCoverage,
		type RoadCoverageResponse
	} from '$lib/api';
	import { onDestroy } from 'svelte';
	import { Latest, Poller } from '$lib/latest';

	// icn/ncn/rcn/lcn, in the order a rider would expect to read them - widest
	// network first - and only shown when the bbox actually has one.
	const NETWORK_LABELS: Record<string, string> = {
		icn: 'International cycle routes',
		ncn: 'National Cycle Network',
		rcn: 'Regional cycle routes',
		lcn: 'Local cycle routes'
	};
	const NETWORK_ORDER = ['icn', 'ncn', 'rcn', 'lcn'];

	// A friendlier label for the highway classes a rider is most likely to
	// see; anything else falls back to the raw OSM tag, which is still a
	// legible word. There is no fixed, small set of classes the way
	// icn/ncn/rcn/lcn is, so this is deliberately not exhaustive.
	const HIGHWAY_LABELS: Record<string, string> = {
		primary: 'Primary roads',
		secondary: 'Secondary roads',
		tertiary: 'Tertiary roads',
		trunk: 'Trunk roads',
		unclassified: 'Unclassified roads',
		residential: 'Residential streets',
		living_street: 'Living streets',
		service: 'Service roads',
		cycleway: 'Cycleways',
		track: 'Tracks',
		path: 'Paths',
		footway: 'Footways',
		bridleway: 'Bridleways',
		pedestrian: 'Pedestrian streets'
	};

	let networkData: CoverageResponse | null = $state(null);
	let networkError: string | null = $state(null);
	let roadData: RoadCoverageResponse | null = $state(null);
	let roadError: string | null = $state(null);
	let job: CoverageBackfillStatus | null = $state(null);
	let jobError: string | null = $state(null);
	// Same teardown gap the archive poller had: clearing the scheduled timer
	// does nothing about a status request already in flight, whose callback
	// would arm a fresh timer after onDestroy had already run.
	// Replaced per run rather than reused. Poller now ends a previous run
	// itself, but a fresh instance per click keeps the intent obvious and
	// matches the archive card - the sibling site that got this right while
	// this one did not, which is how a second overlapping poll was reachable
	// from a double click.
	let jobPoll = new Poller();
	// The archive card's other half of that same fix, missing here until now:
	// a fresh Poller per click stops the OLD poller from continuing, but does
	// nothing about the shared `job` variable itself. The backfill button was
	// only ever disabled for 'running', not 'queued' - the state a job is
	// actually in for a beat after the POST returns, before the one shared
	// worker (services/way_matching.py) has picked it up - so a second click
	// in that window starts a second job. Whichever POST's response lands
	// LAST then wins `job`, even if it was the FIRST click's job that lands
	// second (nothing about network timing is FIFO) - silently discarding
	// the newer job's progress and tracking a stale one instead. `jobOwner`
	// closes it exactly like `archiveOwner` does for the archive card: only
	// the click that is still current when its response lands may apply it.
	const jobOwner = new Latest();

	onDestroy(() => jobPoll.stop());

	async function load() {
		try {
			networkData = await coverage.cycleNetwork();
		} catch (err) {
			networkError = err instanceof Error ? err.message : 'Could not load coverage';
		}
		try {
			roadData = await coverage.roads();
		} catch (err) {
			roadError = err instanceof Error ? err.message : 'Could not load road coverage';
		}
	}
	load();

	async function runBackfill() {
		const token = jobOwner.claim();
		jobError = null;
		jobPoll.stop();
		jobPoll = new Poller();
		try {
			const started = await coverage.backfill();
			if (!jobOwner.isCurrent(token)) return;
			job = started;
			void pollJob(token);
		} catch (err) {
			if (!jobOwner.isCurrent(token)) return;
			jobError = err instanceof Error ? err.message : 'Could not start matching';
		}
	}

	async function pollJob(token: number) {
		const poller = jobPoll;
		await poller.run(async () => {
			if (!jobOwner.isCurrent(token) || !job) return false;
			try {
				const next = await coverage.backfillStatus(job.id);
				if (!jobOwner.isCurrent(token)) return false;
				job = next;
			} catch (err) {
				if (!jobOwner.isCurrent(token)) return false;
				if (err instanceof ApiError && err.status === 404) {
					job = null;
				} else {
					jobError = err instanceof Error ? err.message : 'Lost track of that match run';
					// Same reason as the archive card: a status frozen at
					// 'running' leaves the button disabled with nothing to
					// clear it.
					job = { ...job, status: 'error', error: jobError };
				}
				return false;
			}
			return job.status === 'queued' || job.status === 'running';
		}, 1500);
		if (!jobOwner.isCurrent(token)) return;
		// A finished backfill is exactly what changed the numbers below, for
		// both denominators - reload them both. Reached however polling ended,
		// including a job that errored, so the card never sits on figures the
		// run has already superseded.
		void load();
	}

	function sortedNetworkRows(current: CoverageResponse | null): NetworkCoverage[] {
		if (!current) return [];
		return current.networks
			.filter((row) => row.total_m > 0)
			.sort((a, b) => NETWORK_ORDER.indexOf(a.network) - NETWORK_ORDER.indexOf(b.network));
	}
	const networkRows: NetworkCoverage[] = $derived(sortedNetworkRows(networkData));

	function sortedHighwayRows(current: RoadCoverageResponse | null): HighwayCoverage[] {
		if (!current) return [];
		// Busiest classes first - there is no fixed, small ordering here the
		// way icn/ncn/rcn/lcn has one, so distance ridden stands in.
		return current.highways.filter((row) => row.total_m > 0).sort((a, b) => b.total_m - a.total_m);
	}
	const roadRows: HighwayCoverage[] = $derived(sortedHighwayRows(roadData));

	function anyAvailable(
		network: CoverageResponse | null,
		road: RoadCoverageResponse | null
	): boolean {
		return Boolean(network?.available) || Boolean(road?.available);
	}

	// Rendered only once there is something to say - either section on its
	// own, or nothing at all before the first response lands.
	const showCard = $derived(Boolean(networkError || roadError || networkData || roadData));
	// One backfill button for both sections: matching a ride's ways
	// improves the numerator for both denominators at once.
	const showBackfill = $derived(anyAvailable(networkData, roadData));
</script>

{#if showCard}
	<div class="card">
		{#if networkError}
			<p class="error">{networkError}</p>
		{:else if networkData && !networkData.available}
			<section class="muted">
				<h4>Cycle network</h4>
				<p>{networkData.reason}</p>
			</section>
		{:else if networkData && networkRows.length > 0}
			<section>
				<h4>Cycle network</h4>
				<div class="rows">
					{#each networkRows as row (row.network)}
						<div class="row">
							<span class="label">{NETWORK_LABELS[row.network] ?? row.network}</span>
							<span class="pct">{Math.round((row.ridden_m / row.total_m) * 100)}%</span>
						</div>
					{/each}
				</div>
			</section>
		{:else if networkData}
			<!-- available is true but every network's total_m is 0 - a real,
			     reachable answer (no signed cycle route within the bbox at all),
			     not the loading state. Without this branch the section silently
			     rendered nothing, which reads as broken rather than "there isn't
			     one here yet". -->
			<section class="muted">
				<h4>Cycle network</h4>
				<p>No signed cycle routes near your rides yet.</p>
			</section>
		{/if}

		{#if roadError}
			<p class="error">{roadError}</p>
		{:else if roadData && !roadData.available}
			<section class="muted">
				<h4>All roads</h4>
				<p>{roadData.reason}</p>
			</section>
		{:else if roadData && roadRows.length > 0}
			<section>
				<h4>All roads</h4>
				<div class="rows">
					{#each roadRows as row (row.highway)}
						<div class="row">
							<span class="label">{HIGHWAY_LABELS[row.highway] ?? row.highway}</span>
							<span class="pct">{Math.round((row.ridden_m / row.total_m) * 100)}%</span>
						</div>
					{/each}
				</div>
			</section>
		{:else if roadData}
			<!-- Same reasoning as the network section's own empty branch above:
			     available with nothing over 0 m is a real answer, not loading. -->
			<section class="muted">
				<h4>All roads</h4>
				<p>No bikeable roads mapped near your rides yet.</p>
			</section>
		{/if}

		{#if showBackfill}
			<button
				type="button"
				onclick={runBackfill}
				disabled={job?.status === 'running' || job?.status === 'queued'}
			>
				{job?.status === 'running' || job?.status === 'queued'
					? `Matching… ${job.matched + job.unmatched} of ${job.total || '?'}`
					: 'Match older rides'}
			</button>
			{#if jobError}
				<p class="error">{jobError}</p>
			{/if}
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
	section {
		margin-bottom: 0.6rem;
	}
	section.muted {
		color: var(--text-muted);
	}
	section:last-of-type {
		margin-bottom: 0.5rem;
	}
	h4 {
		margin: 0 0 0.3rem;
		font-size: 0.75rem;
		font-weight: 600;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}
	.rows {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
	}
	.row {
		display: flex;
		justify-content: space-between;
		gap: 1rem;
	}
	.label {
		color: var(--text);
	}
	.pct {
		font-weight: 600;
		color: var(--link);
	}
	.error {
		color: var(--danger-text);
	}
	button {
		font-size: 0.85rem;
		padding: 0.3rem 0.7rem;
		border: 1px solid var(--input-border);
		border-radius: 6px;
		background: var(--surface);
		color: var(--text);
		cursor: pointer;
	}
</style>
