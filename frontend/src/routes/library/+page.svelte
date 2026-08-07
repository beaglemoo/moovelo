<script lang="ts">
	import { goto } from '$app/navigation';
	import { routes, wahoo, type RouteQuery, type RouteSummary, type WahooStatus } from '$lib/api';
	import ImportResults from '$lib/components/ImportResults.svelte';
	import { km } from '$lib/format';
	import { ACCEPTED_FILES, importQueue } from '$lib/import.svelte';
	import { onDestroy } from 'svelte';

	let fileInput: HTMLInputElement | undefined = $state();

	async function chooseFiles(event: Event) {
		const input = event.currentTarget as HTMLInputElement;
		if (!input.files?.length) return;
		await importQueue.add(input.files);
		// Let the same file be picked again after a failed attempt.
		input.value = '';
	}

	// Imports also start from the window-wide drop target in the layout, so
	// the list refreshes on the queue finishing rather than on this page
	// having been the thing that started it.
	let seenBatch = 0;
	$effect(() => {
		const batch = importQueue.completedBatches;
		if (batch !== seenBatch) {
			seenBatch = batch;
			void refresh();
		}
	});

	let items: RouteSummary[] = $state([]);
	let loaded = $state(false);
	let renamingId: string | null = $state(null);
	let renameValue = $state('');
	let error: string | null = $state(null);
	let wahooStatus: WahooStatus | null = $state(null);
	let pollTimer: ReturnType<typeof setTimeout> | null = null;

	let query: RouteQuery = $state({ sort: 'updated', order: 'desc' });
	let search = $state('');
	let allTags: string[] = $state([]);
	let searchTimer: ReturnType<typeof setTimeout> | null = null;

	// Debounced so typing does not fire a request per keystroke.
	function onSearchInput() {
		if (searchTimer) clearTimeout(searchTimer);
		searchTimer = setTimeout(() => {
			query = { ...query, q: search.trim() || undefined };
			void refresh();
		}, 250);
	}

	function setQuery(patch: Partial<RouteQuery>) {
		query = { ...query, ...patch };
		void refresh();
	}

	async function refresh() {
		try {
			items = await routes.list(query);
			loaded = true;
			schedulePoll();
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to load routes';
		}
	}
	refresh();
	routes.tags().then((t) => (allTags = t));
	wahoo.status().then((s) => (wahooStatus = s));

	// Keep refreshing while any push is in flight.
	function schedulePoll() {
		if (pollTimer) clearTimeout(pollTimer);
		if (items.some((r) => r.wahoo.status === 'queued' || r.wahoo.status === 'pushing')) {
			pollTimer = setTimeout(refresh, 3000);
		}
	}
	onDestroy(() => {
		if (pollTimer) clearTimeout(pollTimer);
		if (searchTimer) clearTimeout(searchTimer);
	});

	async function sendToWahoo(item: RouteSummary) {
		try {
			const updated = await wahoo.push(item.id);
			items = items.map((r) => (r.id === updated.id ? updated : r));
			schedulePoll();
		} catch (err) {
			error = err instanceof Error ? err.message : 'Push failed';
		}
	}

	async function disconnectWahoo() {
		if (!confirm('Disconnect your Wahoo account?')) return;
		await wahoo.disconnect();
		wahooStatus = await wahoo.status();
	}

	function badge(item: RouteSummary): { label: string; cls: string; title: string } {
		switch (item.wahoo.status) {
			case 'queued':
				return { label: 'queued', cls: 'pending', title: 'Waiting to push' };
			case 'pushing':
				return { label: 'pushing', cls: 'pending', title: 'Uploading to Wahoo' };
			case 'synced':
				return {
					label: 'synced',
					cls: 'ok',
					title: item.wahoo.pushed_at
						? `Pushed ${new Date(item.wahoo.pushed_at).toLocaleString()}`
						: 'Pushed'
				};
			case 'error':
				return { label: 'error', cls: 'bad', title: item.wahoo.error ?? 'Push failed' };
			default:
				return { label: '', cls: '', title: '' };
		}
	}

	function formatDate(iso: string): string {
		return new Date(iso).toLocaleDateString(undefined, {
			day: 'numeric',
			month: 'short',
			year: 'numeric'
		});
	}

	function startRename(item: RouteSummary) {
		renamingId = item.id;
		renameValue = item.name;
	}

	async function commitRename() {
		if (renamingId && renameValue.trim()) {
			await routes.update(renamingId, { name: renameValue.trim() });
			await refresh();
		}
		renamingId = null;
	}

	async function toggleFavourite(item: RouteSummary) {
		try {
			const updated = await routes.update(item.id, { is_favourite: !item.is_favourite });
			items = items.map((r) =>
				r.id === item.id ? { ...r, is_favourite: updated.is_favourite } : r
			);
		} catch (err) {
			error = err instanceof Error ? err.message : 'Could not update that route';
		}
	}

	let taggingId: string | null = $state(null);
	let tagValue = $state('');

	function startTagging(item: RouteSummary) {
		taggingId = item.id;
		tagValue = item.tags.join(', ');
	}

	const filtered = $derived(Boolean(query.q || query.tag || query.favourite || query.source));

	async function commitTags() {
		if (!taggingId) return;
		const id = taggingId;
		taggingId = null;
		const tags = tagValue
			.split(',')
			.map((t) => t.trim())
			.filter(Boolean);
		try {
			const updated = await routes.update(id, { tags });
			items = items.map((r) => (r.id === id ? { ...r, tags: updated.tags } : r));
			allTags = await routes.tags();
		} catch (err) {
			// Put the edit back in the box rather than losing what was typed.
			taggingId = id;
			error = err instanceof Error ? err.message : 'Could not save those tags';
		}
	}

	let working: string | null = $state(null);

	async function duplicate(item: RouteSummary) {
		working = item.id;
		try {
			await routes.duplicate(item.id);
			await refresh();
		} catch (err) {
			error = err instanceof Error ? err.message : 'Duplicate failed';
		} finally {
			working = null;
		}
	}

	async function reverse(item: RouteSummary) {
		working = item.id;
		try {
			await routes.reverse(item.id);
			await refresh();
		} catch (err) {
			error = err instanceof Error ? err.message : 'Reverse failed';
		} finally {
			working = null;
		}
	}

	async function remove(item: RouteSummary) {
		if (!confirm(`Delete "${item.name}"?`)) return;
		await routes.remove(item.id);
		await refresh();
	}

	let copiedId: string | null = $state(null);

	async function share(item: RouteSummary) {
		try {
			const updated = item.share_token ? item : await routes.share(item.id);
			items = items.map((r) => (r.id === updated.id ? { ...r, ...updated } : r));
			const url = `${location.origin}/s/${updated.share_token}`;
			await navigator.clipboard.writeText(url);
			copiedId = item.id;
			setTimeout(() => (copiedId = null), 2000);
		} catch (err) {
			error = err instanceof Error ? err.message : 'Share failed';
		}
	}

	async function unshare(item: RouteSummary) {
		if (!confirm('Revoke the share link? Anyone with the old link loses access.')) return;
		const updated = await routes.revokeShare(item.id);
		items = items.map((r) => (r.id === updated.id ? { ...r, ...updated } : r));
	}
</script>

<div class="page">
	<div class="header">
		<h1>Library</h1>
		<button
			type="button"
			class="import"
			onclick={() => fileInput?.click()}
			disabled={importQueue.busy}
		>
			{importQueue.busy ? 'Importing…' : 'Import'}
		</button>
		<input
			bind:this={fileInput}
			type="file"
			accept={ACCEPTED_FILES}
			multiple
			hidden
			onchange={chooseFiles}
		/>
		{#if wahooStatus?.configured}
			{#if wahooStatus.connected}
				<span class="wahoo-connected">
					Wahoo: {wahooStatus.athlete?.name ?? 'connected'}
					<button type="button" onclick={disconnectWahoo}>Disconnect</button>
				</span>
			{:else}
				<a class="wahoo-connect" href={wahoo.connectUrl}>Connect Wahoo</a>
			{/if}
		{/if}
	</div>
	<ImportResults />
	<div class="filters">
		<input
			type="search"
			placeholder="Search names and notes"
			bind:value={search}
			oninput={onSearchInput}
		/>
		<select
			value={query.tag ?? ''}
			onchange={(e) => setQuery({ tag: e.currentTarget.value || undefined })}
		>
			<option value="">All tags</option>
			{#each allTags as tag (tag)}
				<option value={tag}>{tag}</option>
			{/each}
		</select>
		<select
			value={query.source ?? ''}
			onchange={(e) =>
				setQuery({ source: (e.currentTarget.value || undefined) as RouteQuery['source'] })}
		>
			<option value="">Planned and imported</option>
			<option value="planned">Planned</option>
			<option value="imported">Imported</option>
		</select>
		<button
			type="button"
			class="toggle"
			class:on={query.favourite}
			aria-pressed={Boolean(query.favourite)}
			onclick={() => setQuery({ favourite: query.favourite ? undefined : true })}
		>
			★ Favourites
		</button>
		<select
			value={`${query.sort}:${query.order}`}
			onchange={(e) => {
				const [sort, order] = e.currentTarget.value.split(':');
				setQuery({ sort: sort as RouteQuery['sort'], order: order as RouteQuery['order'] });
			}}
		>
			<option value="updated:desc">Newest first</option>
			<option value="updated:asc">Oldest first</option>
			<option value="name:asc">Name A-Z</option>
			<option value="name:desc">Name Z-A</option>
			<option value="distance:desc">Longest first</option>
			<option value="distance:asc">Shortest first</option>
			<option value="ascent:desc">Most climbing</option>
			<option value="ascent:asc">Least climbing</option>
		</select>
	</div>
	{#if error}
		<p class="error">{error}</p>
	{:else if loaded && items.length === 0 && filtered}
		<p class="empty">
			Nothing matches those filters.
			<button
				type="button"
				class="link"
				onclick={() => {
					search = '';
					query = { sort: query.sort, order: query.order };
					void refresh();
				}}>Clear filters</button
			>
		</p>
	{:else if loaded && items.length === 0}
		<p class="empty">
			No saved routes yet. <a href="/">Plan one</a> and hit Save, or import a GPX, TCX or FIT file.
		</p>
	{:else if loaded}
		<table>
			<thead>
				<tr>
					<th>Name</th>
					<th>Preset</th>
					<th>Distance</th>
					<th>Ascent</th>
					<th>Updated</th>
					<th></th>
				</tr>
			</thead>
			<tbody>
				{#each items as item (item.id)}
					<tr>
						<td>
							<button
								type="button"
								class="star"
								class:on={item.is_favourite}
								aria-pressed={item.is_favourite}
								title={item.is_favourite ? 'Remove from favourites' : 'Add to favourites'}
								onclick={() => toggleFavourite(item)}
							>
								{item.is_favourite ? '★' : '☆'}
							</button>
							{#if renamingId === item.id}
								<!-- svelte-ignore a11y_autofocus -->
								<input
									bind:value={renameValue}
									autofocus
									onblur={commitRename}
									onkeydown={(e) => {
										if (e.key === 'Enter') commitRename();
										if (e.key === 'Escape') renamingId = null;
									}}
								/>
							{:else}
								<button type="button" class="name" onclick={() => goto(`/?route=${item.id}`)}>
									{item.name}
								</button>
								{#if item.source === 'imported'}
									<span class="imported" title="Imported from a file">imported</span>
								{/if}
							{/if}
							<div class="tags">
								{#if taggingId === item.id}
									<!-- svelte-ignore a11y_autofocus -->
									<input
										class="tag-input"
										bind:value={tagValue}
										autofocus
										placeholder="comma, separated, tags"
										onblur={commitTags}
										onkeydown={(e) => {
											if (e.key === 'Enter') commitTags();
											if (e.key === 'Escape') taggingId = null;
										}}
									/>
								{:else}
									{#each item.tags as tag (tag)}
										<span class="tag">{tag}</span>
									{/each}
									<button type="button" class="tag-edit" onclick={() => startTagging(item)}>
										{item.tags.length ? 'edit tags' : '+ tag'}
									</button>
								{/if}
							</div>
						</td>
						<td class="muted">{item.preset}</td>
						<td>{km(item.distance_m)}</td>
						<td>{Math.round(item.ascent_m)} m</td>
						<td class="muted">{formatDate(item.updated_at)}</td>
						<td class="actions">
							<a href={routes.gpxUrl(item.id)} download>GPX</a>
							<a href={routes.fitUrl(item.id)} download>FIT</a>
							{#if wahooStatus?.connected}
								<button
									type="button"
									onclick={() => sendToWahoo(item)}
									disabled={item.wahoo.status === 'queued' || item.wahoo.status === 'pushing'}
								>
									Send to Wahoo
								</button>
							{/if}
							{#if badge(item).label}
								{@const b = badge(item)}
								<span class="badge {b.cls}" title={b.title}>{b.label}</span>
							{/if}
							<button type="button" onclick={() => share(item)}>
								{copiedId === item.id ? 'Copied!' : item.share_token ? 'Copy link' : 'Share'}
							</button>
							{#if item.share_token}
								<button
									type="button"
									class="danger"
									onclick={() => unshare(item)}
									title="Revoke share link"
								>
									Unshare
								</button>
							{/if}
							<button type="button" onclick={() => startRename(item)}>Rename</button>
							<button type="button" disabled={working === item.id} onclick={() => duplicate(item)}>
								Duplicate
							</button>
							<button
								type="button"
								disabled={working === item.id}
								title="Create a new route going the other way"
								onclick={() => reverse(item)}
							>
								{working === item.id ? 'Working…' : 'Reverse'}
							</button>
							<button type="button" class="danger" onclick={() => remove(item)}>Delete</button>
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}
</div>

<style>
	.filters {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
		margin-bottom: 0.9rem;
		align-items: center;
	}
	.filters input[type='search'] {
		flex: 1 1 14rem;
		min-width: 10rem;
		padding: 0.3rem 0.5rem;
		font: inherit;
		font-size: 0.9rem;
	}
	.filters select,
	.toggle {
		font: inherit;
		font-size: 0.85rem;
		padding: 0.3rem 0.5rem;
		border: 1px solid #c8c8c8;
		border-radius: 6px;
		background: #fff;
		cursor: pointer;
	}
	.toggle.on {
		background: #fff8e1;
		border-color: #e0a800;
		color: #7a5b00;
	}
	.link {
		border: none;
		background: none;
		padding: 0;
		color: #0b6fa4;
		cursor: pointer;
		font: inherit;
		text-decoration: underline;
	}
	.star {
		border: none;
		background: none;
		cursor: pointer;
		font-size: 1.05rem;
		color: #b8b8b8;
		padding: 0 0.2rem 0 0;
		line-height: 1;
	}
	.star.on {
		color: #e0a800;
	}
	.tags {
		display: flex;
		flex-wrap: wrap;
		gap: 0.3rem;
		align-items: center;
		margin-top: 0.2rem;
	}
	.tag {
		background: #eef2f4;
		border-radius: 10px;
		padding: 0.05rem 0.5rem;
		font-size: 0.75rem;
		color: #37474f;
	}
	.tag-edit {
		border: none;
		background: none;
		padding: 0;
		font-size: 0.75rem;
		color: #708090;
		cursor: pointer;
	}
	.tag-input {
		font-size: 0.78rem;
		padding: 0.1rem 0.3rem;
		width: 100%;
		max-width: 18rem;
	}
	.imported {
		font-size: 0.7rem;
		background: #e8f0e8;
		color: #33691e;
		border-radius: 10px;
		padding: 0.05rem 0.45rem;
		margin-left: 0.35rem;
	}
	.import {
		margin-left: auto;
	}

	.page {
		max-width: 900px;
		margin: 0 auto;
		padding: 1.5rem 1rem;
	}
	h1 {
		font-size: 1.3rem;
		color: #073642;
	}
	.header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
	}
	.wahoo-connect {
		border: 1px solid #268bd2;
		color: #268bd2;
		border-radius: 8px;
		padding: 0.4rem 0.8rem;
		font-size: 0.9rem;
		text-decoration: none;
	}
	.wahoo-connected {
		font-size: 0.85rem;
		color: #586e75;
		display: flex;
		gap: 0.6rem;
		align-items: center;
	}
	.wahoo-connected button {
		background: none;
		border: none;
		padding: 0;
		font: inherit;
		font-size: 0.85rem;
		color: #dc322f;
		cursor: pointer;
	}
	.badge {
		font-size: 0.75rem;
		border-radius: 10px;
		padding: 0.1rem 0.5rem;
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}
	.badge.pending {
		background: #b5890033;
		color: #b58900;
	}
	.badge.ok {
		background: #85990033;
		color: #859900;
	}
	.badge.bad {
		background: #dc322f33;
		color: #dc322f;
	}
	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.95rem;
	}
	th {
		text-align: left;
		font-size: 0.8rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: #93a1a1;
		padding: 0.4rem 0.6rem;
		border-bottom: 2px solid #eee8d5;
	}
	td {
		padding: 0.55rem 0.6rem;
		border-bottom: 1px solid #eee8d5;
	}
	.name {
		background: none;
		border: none;
		padding: 0;
		font: inherit;
		font-weight: 600;
		color: #268bd2;
		cursor: pointer;
	}
	.muted {
		color: #93a1a1;
	}
	.actions {
		display: flex;
		gap: 0.6rem;
		align-items: center;
	}
	.actions a {
		color: #268bd2;
		font-size: 0.85rem;
		text-decoration: none;
	}
	.actions button {
		background: none;
		border: none;
		padding: 0;
		font: inherit;
		font-size: 0.85rem;
		color: #586e75;
		cursor: pointer;
	}
	.actions button.danger {
		color: #dc322f;
	}
	td input {
		font: inherit;
		padding: 0.2rem 0.4rem;
		border: 1px solid #268bd2;
		border-radius: 4px;
	}
	.empty,
	.error {
		color: #586e75;
	}
	.error {
		color: #dc322f;
	}
	/* Collapse table rows into cards on small screens. */
	@media (max-width: 640px) {
		thead {
			display: none;
		}
		table,
		tbody {
			display: block;
		}
		tr {
			display: flex;
			flex-wrap: wrap;
			align-items: baseline;
			gap: 0.2rem 0.8rem;
			border: 1px solid #eee8d5;
			border-radius: 10px;
			padding: 0.6rem 0.8rem;
			margin-bottom: 0.8rem;
		}
		td {
			border: none;
			padding: 0;
		}
		td:first-child {
			flex-basis: 100%;
			font-size: 1.05rem;
		}
		.actions {
			flex-basis: 100%;
			flex-wrap: wrap;
			padding-top: 0.3rem;
			gap: 0.5rem 0.8rem;
		}
	}
</style>
