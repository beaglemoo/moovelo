<script lang="ts">
	import { goto } from '$app/navigation';
	import { routes, type RouteSummary } from '$lib/api';

	let items: RouteSummary[] = $state([]);
	let loaded = $state(false);
	let renamingId: string | null = $state(null);
	let renameValue = $state('');
	let error: string | null = $state(null);

	async function refresh() {
		try {
			items = await routes.list();
			loaded = true;
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to load routes';
		}
	}
	refresh();

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
	<h1>Library</h1>
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
