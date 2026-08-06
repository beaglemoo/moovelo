<script lang="ts">
	import { goto } from '$app/navigation';
	import { routes, wahoo, type RouteSummary, type WahooStatus } from '$lib/api';
	import { onDestroy } from 'svelte';

	let items: RouteSummary[] = $state([]);
	let loaded = $state(false);
	let renamingId: string | null = $state(null);
	let renameValue = $state('');
	let error: string | null = $state(null);
	let wahooStatus: WahooStatus | null = $state(null);
	let pollTimer: ReturnType<typeof setTimeout> | null = null;

	async function refresh() {
		try {
			items = await routes.list();
			loaded = true;
			schedulePoll();
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to load routes';
		}
	}
	refresh();
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

	function km(m: number): string {
		return `${(m / 1000).toFixed(1)} km`;
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

	async function remove(item: RouteSummary) {
		if (!confirm(`Delete "${item.name}"?`)) return;
		await routes.remove(item.id);
		await refresh();
	}
</script>

<div class="page">
	<div class="header">
		<h1>Library</h1>
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
	{#if error}
		<p class="error">{error}</p>
	{:else if loaded && items.length === 0}
		<p class="empty">
			No saved routes yet. <a href="/">Plan one</a> and hit Save.
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
							{/if}
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
							<button type="button" onclick={() => startRename(item)}>Rename</button>
							<button type="button" class="danger" onclick={() => remove(item)}>Delete</button>
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}
</div>

<style>
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
</style>
