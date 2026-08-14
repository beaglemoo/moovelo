<script lang="ts">
	import { activities, type ActivityYearStats } from '$lib/api';
	import { distance, duration, elevation } from '$lib/format';
	import { units } from '$lib/units.svelte';
	import { SvelteSet } from 'svelte/reactivity';

	// Same shape as ClimbLogCard and CoverageCard: fetch once on mount, show
	// nothing at all (not an empty card) until there is something to say.
	let years: ActivityYearStats[] = $state([]);
	let error: string | null = $state(null);
	let loaded = $state(false);
	// Which years are expanded to their month breakdown. Keyed on the year
	// number - the undated bucket (year null) never expands, since it has no
	// months to break down. A reactive Set so mutating it in place (add/
	// delete) is itself tracked, unlike a plain Set reassigned each time.
	const expanded = new SvelteSet<number>();

	async function load() {
		try {
			years = (await activities.stats()).years;
		} catch (err) {
			error = err instanceof Error ? err.message : 'Could not load ride stats';
		} finally {
			loaded = true;
		}
	}
	load();

	function yearLabel(year: number | null): string {
		return year === null ? 'Undated' : String(year);
	}

	function monthLabel(month: number): string {
		return new Date(2000, month - 1, 1).toLocaleDateString(undefined, { month: 'short' });
	}

	function toggle(row: ActivityYearStats) {
		if (row.year === null || row.months.length === 0) return;
		const year = row.year;
		if (expanded.has(year)) expanded.delete(year);
		else expanded.add(year);
	}

	const showCard = $derived(loaded && (years.length > 0 || error !== null));
</script>

{#if showCard}
	<div class="card">
		<h4>Riding totals</h4>
		{#if error}
			<p class="error">{error}</p>
		{:else}
			<table>
				<thead>
					<tr>
						<th>Year</th>
						<th>Rides</th>
						<th>Distance</th>
						<th>Moving</th>
						<th>Ascent</th>
					</tr>
				</thead>
				<tbody>
					{#each years as row (row.year ?? 'undated')}
						{@const canExpand = row.year !== null && row.months.length > 0}
						<tr
							class:expandable={canExpand}
							onclick={() => toggle(row)}
							role={canExpand ? 'button' : undefined}
							tabindex={canExpand ? 0 : undefined}
							onkeydown={(e) => {
								if (canExpand && (e.key === 'Enter' || e.key === ' ')) {
									e.preventDefault();
									toggle(row);
								}
							}}
						>
							<td>
								{#if canExpand}
									<span class="chevron"
										>{row.year !== null && expanded.has(row.year) ? '▾' : '▸'}</span
									>
								{/if}
								{yearLabel(row.year)}
							</td>
							<td>{row.count}</td>
							<td>{distance(row.distance_m, units.system)}</td>
							<td>{duration(row.moving_time_s)}</td>
							<td>{elevation(row.ascent_m, units.system)}</td>
						</tr>
						{#if row.year !== null && expanded.has(row.year)}
							{#each row.months as month (month.month)}
								<tr class="month">
									<td>{monthLabel(month.month)}</td>
									<td>{month.count}</td>
									<td>{distance(month.distance_m, units.system)}</td>
									<td>{duration(month.moving_time_s)}</td>
									<td>{elevation(month.ascent_m, units.system)}</td>
								</tr>
							{/each}
						{/if}
					{/each}
				</tbody>
			</table>
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
	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.85rem;
	}
	th {
		text-align: left;
		font-weight: 600;
		color: var(--text-muted);
		border-bottom: 1px solid var(--border);
		padding: 0.25rem 0.4rem;
	}
	td {
		padding: 0.3rem 0.4rem;
		color: var(--text);
	}
	tr.expandable {
		cursor: pointer;
	}
	tr.expandable:hover {
		background: var(--surface-sunken);
	}
	tr.month td {
		color: var(--text-muted);
		padding-left: 1.2rem;
	}
	.chevron {
		display: inline-block;
		width: 1em;
		color: var(--text-muted);
	}
	.error {
		color: var(--danger-text);
		margin: 0;
	}
</style>
