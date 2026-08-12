<script lang="ts">
	import {
		activities,
		ApiError,
		type ActivityQuery,
		type ActivitySummary,
		type ArchiveImportStatus
	} from '$lib/api';
	import { Latest, Poller } from '$lib/latest';
	import { onDestroy } from 'svelte';
	import CoverageCard from '$lib/components/CoverageCard.svelte';
	import ImportResults from '$lib/components/ImportResults.svelte';
	import { km } from '$lib/format';
	import { ACCEPTED_FILES, activityImportQueue } from '$lib/import.svelte';

	let fileInput: HTMLInputElement | undefined = $state();
	let items: ActivitySummary[] = $state([]);
	let loaded = $state(false);
	let error: string | null = $state(null);
	let query: ActivityQuery = $state({});

	// A bulk export is one file that becomes hundreds of rides, so it takes a
	// different path from a single upload: submit, then poll a job. Kept apart
	// from the per-file queue rather than folded into it, because the two
	// report progress in genuinely different units.
	let archive: ArchiveImportStatus | null = $state(null);
	let archiveError: string | null = $state(null);
	// Stops for good on unmount, including a status request already in
	// flight at that moment - clearing only the scheduled timer would let
	// that request's own callback arm a fresh one nothing is left to clear,
	// and the page would go on polling invisibly after it was gone.
	// Replaced per run rather than reused: stop() is permanent, and starting a
	// second import must end the first one's polling for good.
	let archivePoll = new Poller();
	// The Import button only disables once `archive` is set from the POST's
	// response, not from the click - so a slow first upload leaves a window
	// where a second can start while the first has no tracked job yet. Whoever
	// claimed last owns the card; an earlier upload's response landing
	// afterwards is discarded rather than reverting the display to a job that
	// was superseded before it ever reported anything.
	const archiveOwner = new Latest();
	// Five independent things call refresh(): mount, the year filter, a
	// delete, an import batch finishing, and an archive finishing. Without an
	// owner the earlier of two overlapping calls can resolve last and put a
	// stale list back on screen.
	const listOwner = new Latest();

	onDestroy(() => archivePoll.stop());

	// Only the single-file queue gated the Import button - an archive still
	// queued or running left it clickable, and the backend has one worker for
	// the whole install, so a second archive submitted mid-import either sat
	// behind the first for no visible reason or, once the queue gained a
	// bound, could 429. Selecting several .zip files also used to fire them
	// all in a loop that only awaited the upload's 202, not the import
	// finishing, so only the last one's status was ever actually tracked -
	// startArchive now awaits the whole job before chooseFiles moves to the
	// next file.
	let archiveBusy = $derived.by(
		() => archive?.status === 'queued' || archive?.status === 'running'
	);

	async function chooseFiles(event: Event) {
		const input = event.currentTarget as HTMLInputElement;
		if (!input.files?.length) return;
		const picked = [...input.files];
		// Let the same file be picked again after a failed attempt.
		input.value = '';

		const zips = picked.filter((file) => file.name.toLowerCase().endsWith('.zip'));
		const singles = picked.filter((file) => !file.name.toLowerCase().endsWith('.zip'));

		if (singles.length) await activityImportQueue.add(singles);
		for (const zip of zips) await startArchive(zip);
	}

	async function startArchive(file: File) {
		const token = archiveOwner.claim();
		archivePoll.stop();
		archivePoll = new Poller();
		archiveError = null;
		try {
			const started = await activities.importArchive(file);
			if (!archiveOwner.isCurrent(token)) return;
			archive = started;
		} catch (err) {
			if (!archiveOwner.isCurrent(token)) return;
			archiveError = err instanceof Error ? err.message : 'Could not read that archive';
			return;
		}
		await pollArchiveUntilDone(token);
	}

	/** Polls until the job leaves queued/running, then resolves - so a
	 * caller awaiting this (chooseFiles, serialising several .zip files) only
	 * moves on once this one has actually finished, not once it was merely
	 * accepted. */
	async function pollArchiveUntilDone(token: number): Promise<void> {
		const poller = archivePoll;
		await poller.run(async () => {
			if (!archiveOwner.isCurrent(token) || !archive) return false;
			if (archive.status !== 'queued' && archive.status !== 'running') return false;
			const jobId = archive.id;
			try {
				const next = await activities.archiveStatus(jobId);
				if (!archiveOwner.isCurrent(token)) return false;
				archive = next;
			} catch (err) {
				if (!archiveOwner.isCurrent(token)) return false;
				// Only a 404 means the job is genuinely gone - a restart, or
				// eviction once enough later jobs finished - and only then is
				// silently dropping the card the right answer. A 500 or a
				// dropped connection used to look identical: the card vanished
				// mid-import with nothing said, which for a multi-minute import
				// of hundreds of files reads as "it lost my rides".
				if (err instanceof ApiError && err.status === 404) {
					archive = null;
				} else {
					archiveError = err instanceof Error ? err.message : 'Lost track of that import';
				}
				return false;
			}
			return true;
		}, 1500);
		if (archiveOwner.isCurrent(token)) void refresh();
	}

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

	{#if archiveError}
		<p class="error">{archiveError}</p>
	{/if}

	{#if archive}
		<div class="archive" class:done={archive.status === 'done'}>
			<strong>{archive.filename}</strong>
			{#if archive.status === 'error'}
				<span class="error">{archive.error}</span>
			{:else if archive.status === 'done'}
				<span>
					{archive.imported} imported
					{#if archive.duplicates}· {archive.duplicates} already had{/if}
					{#if archive.skipped}· {archive.skipped} not rides{/if}
					{#if archive.failed}· {archive.failed} failed{/if}
				</span>
				<button type="button" onclick={() => (archive = null)}>Dismiss</button>
			{:else}
				<span>
					reading… {archive.imported + archive.failed} of {archive.total || '?'}
				</span>
			{/if}
		</div>
		{#if archive.problems.length}
			<ul class="problems">
				{#each archive.problems as problem (problem)}
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
