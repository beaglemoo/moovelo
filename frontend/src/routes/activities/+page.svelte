<script lang="ts">
	import { activities, type ActivityQuery, type ActivitySummary } from '$lib/api';
	import { Latest } from '$lib/latest';
	import CoverageCard from '$lib/components/CoverageCard.svelte';
	import ImportResults from '$lib/components/ImportResults.svelte';
	import { km } from '$lib/format';
	import {
		ACCEPTED_FILES,
		activityImportQueue,
		archiveImport,
		pendingArchives
	} from '$lib/import.svelte';

	let fileInput: HTMLInputElement | undefined = $state();
	let items: ActivitySummary[] = $state([]);
	let loaded = $state(false);
	let error: string | null = $state(null);
	let query: ActivityQuery = $state({});

	// A bulk export is one file that becomes hundreds of rides, so it takes a
	// different path from a single upload: submit, then poll a job. The whole
	// attempt/poll/ownership machinery now lives in a module singleton
	// (archiveImport) rather than component-local state, so it survives
	// navigation - leaving this page mid-import and coming back used to
	// destroy the card and re-enable the Import button while the backend job
	// ran on. These are read-only bridges to it; the phase design and its long
	// history live where the state does now, in import.svelte.ts.
	let attempt = $derived(archiveImport.attempt);
	let archiveBusy = $derived(archiveImport.busy);

	// Five independent things call refresh(): mount, the year filter, a
	// delete, an import batch finishing, and an archive finishing. Without an
	// owner the earlier of two overlapping calls can resolve last and put a
	// stale list back on screen.
	const listOwner = new Latest();

	async function chooseFiles(event: Event) {
		const input = event.currentTarget as HTMLInputElement;
		if (!input.files?.length) return;
		const picked = [...input.files];
		// Let the same file be picked again after a failed attempt.
		input.value = '';

		const zips = picked.filter((file) => file.name.toLowerCase().endsWith('.zip'));
		const singles = picked.filter((file) => !file.name.toLowerCase().endsWith('.zip'));

		if (singles.length) await activityImportQueue.add(singles);
		for (const zip of zips) await archiveImport.start(zip);
	}

	// A .zip dropped on this page lands here: the layout's drop handler has no
	// archive machinery of its own, so it leaves the file for whoever can
	// actually show its progress.
	$effect(() => {
		if (!pendingArchives.files.length) return;
		const waiting = pendingArchives.drain();
		void (async () => {
			for (const zip of waiting) await archiveImport.start(zip);
		})();
	});

	// An archive finishing refreshes the list, the same signal shape the
	// single-file queue gives via completedBatches. The counter lives on the
	// module singleton, so a completion that lands while this page is unmounted
	// is caught by the mount-time refresh() below rather than lost.
	let seenArchive = 0;
	$effect(() => {
		const done = archiveImport.completed;
		if (done !== seenArchive) {
			seenArchive = done;
			void refresh();
		}
	});

	// Same shape as the library's: the queue finishing is what refreshes the
	// list, not this page having been the thing that started it.
	let seenBatch = 0;
	$effect(() => {
		const batch = activityImportQueue.completedBatches;
		if (batch !== seenBatch) {
			seenBatch = batch;
			void refresh();
		}
	});

	async function refresh() {
		const token = listOwner.claim();
		try {
			const rows = await activities.list(query);
			// A newer refresh started while this one was in flight, so this
			// answer is already out of date - applying it would put the
			// previous filter's rows back on screen.
			if (!listOwner.isCurrent(token)) return;
			items = rows;
			loaded = true;
		} catch (err) {
			if (!listOwner.isCurrent(token)) return;
			error = err instanceof Error ? err.message : 'Failed to load activities';
		}
	}
	refresh();

	function setQuery(patch: Partial<ActivityQuery>) {
		query = { ...query, ...patch };
		void refresh();
	}

	async function remove(item: ActivitySummary) {
		if (!confirm(`Delete "${item.name}"? This cannot be undone.`)) return;
		try {
			await activities.remove(item.id);
			await refresh();
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to delete';
		}
	}

	/** Years present in the loaded list, so the filter never offers a year
	 * with nothing behind it. */
	const years = $derived([
		...new Set(
			items
				.map((item) => item.started_at?.slice(0, 4))
				.filter((year): year is string => year !== undefined)
		)
	]);

	const totals = $derived({
		count: items.length,
		distance_m: items.reduce((sum, item) => sum + item.distance_m, 0),
		ascent_m: items.reduce((sum, item) => sum + item.ascent_m, 0)
	});

	function rideDate(item: ActivitySummary): string {
		// Undated rides show when they were imported rather than an empty
		// cell, which reads as a bug.
		const stamp = item.started_at ?? item.created_at;
		const label = new Date(stamp).toLocaleDateString(undefined, {
			day: 'numeric',
			month: 'short',
			year: 'numeric'
		});
		return item.started_at ? label : `imported ${label}`;
	}

	function duration(seconds: number | null): string {
		if (seconds === null) return '-';
		const hours = Math.floor(seconds / 3600);
		const minutes = Math.round((seconds % 3600) / 60);
		return hours > 0 ? `${hours}h ${minutes}m` : `${minutes}m`;
	}
</script>

<svelte:head><title>Activities - Moovelo</title></svelte:head>

<div class="page">
	<div class="head">
		<h1>Activities</h1>
		<input
			bind:this={fileInput}
			type="file"
			multiple
			accept={`${ACCEPTED_FILES},.zip`}
			onchange={chooseFiles}
			hidden
		/>
		<button
			type="button"
			class="import"
			onclick={() => fileInput?.click()}
			disabled={activityImportQueue.busy || archiveBusy}
		>
			{activityImportQueue.busy || archiveBusy ? 'Importing…' : 'Import rides'}
		</button>
	</div>

	{#if error}
		<p class="error">{error}</p>
	{/if}

	<!-- There is no page-level archive error any more, and that is the fix
	     rather than an omission. An error beside the card was a second place
	     the same failure could appear, which needed a rule to decide when to
	     show it - first a comparison of message text (which hid a real,
	     different failure whose text happened to match), then a check on the
	     card's status. The failure belongs to an attempt, so it renders as
	     that attempt's phase, in the one place the attempt is shown. Nothing
	     left to relate, so nothing left to relate wrongly. -->
	{#if attempt}
		<div class="archive" class:done={attempt.phase === 'tracking' && attempt.job.status === 'done'}>
			<strong>{attempt.phase === 'tracking' ? attempt.job.filename : attempt.filename}</strong>
			{#if attempt.phase === 'uploading'}
				<span>uploading…</span>
			{:else if attempt.phase === 'failed'}
				<span class="error">{attempt.message}</span>
				<button type="button" onclick={() => archiveImport.dismiss()}>Dismiss</button>
			{:else if attempt.job.status === 'error'}
				<span class="error">{attempt.job.error}</span>
				<button type="button" onclick={() => archiveImport.dismiss()}>Dismiss</button>
			{:else if attempt.job.status === 'done'}
				<span>
					{attempt.job.imported} imported
					{#if attempt.job.duplicates}· {attempt.job.duplicates} already had{/if}
					{#if attempt.job.skipped}· {attempt.job.skipped} not rides{/if}
					{#if attempt.job.failed}· {attempt.job.failed} failed{/if}
				</span>
				<button type="button" onclick={() => archiveImport.dismiss()}>Dismiss</button>
			{:else}
				<span>
					reading… {attempt.job.imported + attempt.job.failed} of {attempt.job.total || '?'}
				</span>
			{/if}
		</div>
		{#if attempt.phase === 'tracking' && attempt.job.problems.length}
			<ul class="problems">
				{#each attempt.job.problems as problem (problem)}
					<li>{problem}</li>
				{/each}
			</ul>
		{/if}
	{/if}

	<ImportResults queue={activityImportQueue} />

	<CoverageCard />

	{#if loaded && items.length > 0}
		<div class="controls">
			<select
				aria-label="Filter by year"
				value={query.year ?? ''}
				onchange={(e) =>
					setQuery({ year: e.currentTarget.value ? Number(e.currentTarget.value) : undefined })}
			>
				<option value="">All years</option>
				{#each years as year (year)}
					<option value={year}>{year}</option>
				{/each}
			</select>
			<span class="totals">
				{totals.count} rides · {km(totals.distance_m)} · {Math.round(totals.ascent_m)} m
			</span>
		</div>

		<table>
			<thead>
				<tr>
					<th>Date</th>
					<th>Name</th>
					<th>Distance</th>
					<th>Moving</th>
					<th>Ascent</th>
					<th></th>
				</tr>
			</thead>
			<tbody>
				{#each items as item (item.id)}
					<tr>
						<td data-label="Date">{rideDate(item)}</td>
						<td data-label="Name">{item.name}</td>
						<td data-label="Distance">{km(item.distance_m)}</td>
						<td data-label="Moving">{duration(item.moving_time_s)}</td>
						<td data-label="Ascent">{Math.round(item.ascent_m)} m</td>
						<td class="actions">
							<button type="button" onclick={() => remove(item)}>Delete</button>
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{:else if loaded && query.year !== undefined}
		<p class="empty">
			Nothing from that year.
			<button type="button" onclick={() => setQuery({ year: undefined })}>Show all</button>
		</p>
	{:else if loaded}
		<p class="empty">
			No rides yet. Import a GPX, TCX or FIT file from a head unit or a Strava export, and it lands
			here - separate from your planned routes, because it is a record of where you actually went.
		</p>
	{/if}
</div>

<style>
	.page {
		max-width: 900px;
		margin: 0 auto;
		padding: 1.5rem 1rem;
	}
	.head {
		display: flex;
		align-items: center;
		gap: 1rem;
	}
	h1 {
		font-size: 1.3rem;
		color: #073642;
		margin: 0;
		flex: 1;
	}
	.import {
		padding: 0.4rem 0.9rem;
	}
	.error {
		color: #dc322f;
	}
	.archive {
		display: flex;
		align-items: center;
		gap: 0.6rem;
		flex-wrap: wrap;
		background: #fdf6e3;
		border: 1px solid #eee8d5;
		border-radius: 6px;
		padding: 0.5rem 0.7rem;
		margin: 0.8rem 0 0.4rem;
		font-size: 0.9rem;
	}
	.archive.done {
		border-color: #859900;
	}
	.problems {
		margin: 0 0 0.8rem;
		padding-left: 1.2rem;
		color: #657b83;
		font-size: 0.85rem;
	}
	.controls {
		display: flex;
		align-items: center;
		gap: 0.8rem;
		margin: 1rem 0 0.5rem;
		flex-wrap: wrap;
	}
	.totals {
		color: #657b83;
		font-size: 0.9rem;
	}
	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.92rem;
	}
	th {
		text-align: left;
		font-weight: 600;
		color: #657b83;
		border-bottom: 1px solid #eee8d5;
		padding: 0.4rem 0.5rem;
	}
	td {
		padding: 0.5rem;
		border-bottom: 1px solid #f2ede0;
	}
	.actions {
		text-align: right;
	}
	.empty {
		color: #657b83;
		max-width: 46ch;
		line-height: 1.5;
	}
	/* Same technique the library uses: the table becomes stacked cards
	   rather than scrolling sideways on a phone. */
	@media (max-width: 640px) {
		table,
		thead,
		tbody,
		tr,
		td {
			display: block;
		}
		thead {
			display: none;
		}
		tr {
			border-bottom: 1px solid #eee8d5;
			padding: 0.5rem 0;
		}
		td {
			border: none;
			padding: 0.15rem 0.5rem;
			display: flex;
			gap: 0.5rem;
		}
		td[data-label]::before {
			content: attr(data-label);
			color: #93a1a1;
			min-width: 5.5rem;
		}
		.actions {
			text-align: left;
		}
	}
</style>
