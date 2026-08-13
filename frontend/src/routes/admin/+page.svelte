<script lang="ts">
	import { admin, type AdminOverview } from '$lib/api';
	import LLMSettingsPanel from '$lib/components/LLMSettingsPanel.svelte';

	let overview: AdminOverview | null = $state(null);
	let error: string | null = $state(null);

	async function refresh() {
		try {
			overview = await admin.overview();
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to load admin data';
		}
	}
	refresh();

	async function removeUser(id: string, email: string) {
		if (!confirm(`Delete ${email} and all their routes?`)) return;
		try {
			await admin.deleteUser(id);
			await refresh();
		} catch (err) {
			error = err instanceof Error ? err.message : 'Delete failed';
		}
	}

	function formatDate(iso: string): string {
		return new Date(iso).toLocaleDateString(undefined, {
			day: 'numeric',
			month: 'short',
			year: 'numeric'
		});
	}

	function onoff(v: boolean): string {
		return v ? 'on' : 'off';
	}
</script>

<div class="page">
	<h1>Admin</h1>
	{#if error}
		<p class="error">{error}</p>
	{/if}
	{#if overview}
		<div class="cards">
			<div class="card">
				<span class="num">{overview.stats.user_count}</span>
				<span class="label">users</span>
			</div>
			<div class="card">
				<span class="num">{overview.stats.route_count}</span>
				<span class="label">routes</span>
			</div>
			<div class="card config">
				<span class="label">configuration</span>
				<ul>
					<li>Signups: {onoff(overview.config.signups_enabled)}</li>
					<li>Password login: {onoff(overview.config.password_login)}</li>
					<li>
						SSO: {overview.config.oidc_enabled ? (overview.config.oidc_provider ?? 'on') : 'off'}
					</li>
					<li>Wahoo: {overview.config.wahoo_configured ? 'configured' : 'not configured'}</li>
					<li>Version: {overview.config.version}</li>
				</ul>
			</div>
		</div>

		<h2>Users</h2>
		<table>
			<thead>
				<tr>
					<th>Email</th>
					<th>Role</th>
					<th>Routes</th>
					<th>Wahoo</th>
					<th>Joined</th>
					<th></th>
				</tr>
			</thead>
			<tbody>
				{#each overview.users as u (u.id)}
					<tr>
						<td class="email">{u.email}</td>
						<td>{u.is_admin ? 'admin' : 'user'}</td>
						<td>{u.route_count}</td>
						<td>{u.wahoo_connected ? 'connected' : '-'}</td>
						<td class="muted">{formatDate(u.created_at)}</td>
						<td>
							{#if !u.is_admin}
								<button type="button" class="danger" onclick={() => removeUser(u.id, u.email)}>
									Delete
								</button>
							{/if}
						</td>
					</tr>
				{/each}
			</tbody>
		</table>

		<LLMSettingsPanel />
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
		color: var(--text);
	}
	h2 {
		font-size: 1.05rem;
		color: var(--text);
		margin-top: 1.6rem;
	}
	.cards {
		display: flex;
		gap: 1rem;
		flex-wrap: wrap;
	}
	.card {
		border: 1px solid var(--border);
		border-radius: 10px;
		padding: 0.9rem 1.2rem;
		background: var(--surface);
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		min-width: 90px;
	}
	.card .num {
		font-size: 1.6rem;
		font-weight: 700;
		color: var(--text);
	}
	.card .label {
		font-size: 0.75rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-muted);
	}
	.card.config ul {
		margin: 0.2rem 0 0;
		padding: 0;
		list-style: none;
		font-size: 0.85rem;
		color: var(--text-muted);
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
		color: var(--text-muted);
		padding: 0.4rem 0.6rem;
		border-bottom: 2px solid var(--border);
	}
	td {
		padding: 0.55rem 0.6rem;
		border-bottom: 1px solid var(--border);
	}
	.email {
		font-weight: 600;
		color: var(--text);
	}
	.muted {
		color: var(--text-muted);
	}
	.danger {
		background: none;
		border: none;
		padding: 0;
		font: inherit;
		font-size: 0.85rem;
		color: var(--danger);
		cursor: pointer;
	}
	.error {
		color: var(--danger);
	}
</style>
