<script lang="ts">
	import { settings as settingsApi, type UserSettings } from '$lib/api';

	let weightKg: number = $state(78);
	let flatSpeedKmh: number = $state(22);
	// undefined is what an empty <input type="number"> binds to - the
	// clearable state for the optional FTP field.
	let ftpWatts: number | undefined = $state(undefined);

	let loading = $state(true);
	let saving = $state(false);
	let saved = $state(false);
	let error: string | null = $state(null);

	function apply(data: UserSettings) {
		weightKg = data.weight_kg;
		flatSpeedKmh = data.flat_speed_kmh;
		ftpWatts = data.ftp_watts ?? undefined;
	}

	async function load() {
		try {
			apply(await settingsApi.get());
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to load settings';
		} finally {
			loading = false;
		}
	}
	load();

	async function save() {
		saving = true;
		saved = false;
		error = null;
		try {
			const updated = await settingsApi.update({
				weight_kg: weightKg,
				flat_speed_kmh: flatSpeedKmh,
				ftp_watts: ftpWatts ?? null
			});
			apply(updated);
			saved = true;
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to save settings';
		} finally {
			saving = false;
		}
	}
</script>

<div class="page">
	<h1>Settings</h1>
	<p class="hint">
		These feed the ride-time estimate shown in the planner and library, rather than changing routing
		itself.
	</p>

	{#if loading}
		<p>Loading…</p>
	{:else}
		<form onsubmit={(e) => (e.preventDefault(), save())}>
			<label>
				<span>Weight (kg)</span>
				<input type="number" min="30" max="200" step="0.5" bind:value={weightKg} required />
			</label>

			<label>
				<span>Flat-road speed (km/h)</span>
				<input type="number" min="5" max="60" step="0.5" bind:value={flatSpeedKmh} required />
			</label>

			<label>
				<span>FTP (W, optional)</span>
				<input
					type="number"
					min="1"
					max="2000"
					step="1"
					bind:value={ftpWatts}
					placeholder="Not set"
				/>
			</label>

			<div class="actions">
				<button type="submit" disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
				{#if saved}
					<span class="saved">Saved</span>
				{/if}
				{#if error}
					<span class="error">{error}</span>
				{/if}
			</div>
		</form>
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
	.hint {
		color: #586e75;
		font-size: 0.9rem;
		max-width: 40rem;
	}
	form {
		display: flex;
		flex-direction: column;
		gap: 1rem;
		max-width: 20rem;
		margin-top: 1.2rem;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
		font-size: 0.9rem;
		color: #073642;
	}
	input {
		font: inherit;
		font-size: 1rem;
		padding: 0.45rem 0.6rem;
		border: 1px solid #eee8d5;
		border-radius: 6px;
		background: #fdf6e3;
		color: #073642;
	}
	.actions {
		display: flex;
		align-items: center;
		gap: 0.8rem;
	}
	button {
		border: none;
		border-radius: 6px;
		padding: 0.5rem 1.1rem;
		font: inherit;
		font-size: 0.95rem;
		background: #073642;
		color: #fdf6e3;
		cursor: pointer;
	}
	button:disabled {
		opacity: 0.6;
		cursor: default;
	}
	.saved {
		color: #859900;
		font-size: 0.9rem;
	}
	.error {
		color: #dc322f;
		font-size: 0.9rem;
	}
</style>
