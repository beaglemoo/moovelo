<script lang="ts">
	import { settings as settingsApi, type RideTimeSuggestion, type UserSettings } from '$lib/api';

	let weightKg: number = $state(78);
	let flatSpeedKmh: number = $state(22);
	// undefined is what an empty <input type="number"> binds to - the
	// clearable state for the optional FTP field.
	let ftpWatts: number | undefined = $state(undefined);
	let appVersion: string = $state('');

	let loading = $state(true);
	let saving = $state(false);
	let saved = $state(false);
	let error: string | null = $state(null);

	// Null when there are fewer than services/ride_calibration.py's floor of
	// usable rides - the card is hidden entirely in that case, not shown
	// empty or disabled.
	let suggestion: RideTimeSuggestion | null = $state(null);
	let applyingSuggestion = $state(false);

	function apply(data: UserSettings) {
		weightKg = data.weight_kg;
		flatSpeedKmh = data.flat_speed_kmh;
		ftpWatts = data.ftp_watts ?? undefined;
		appVersion = data.version;
	}

	async function load() {
		try {
			apply(await settingsApi.get());
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to load settings';
		} finally {
			loading = false;
		}
		// A separate call, and never allowed to fail page load: a rider whose
		// suggestion query errors (or who simply has too few matched rides)
		// still gets a working settings page, just without the card.
		try {
			suggestion = await settingsApi.rideTimeSuggestion();
		} catch {
			suggestion = null;
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

	async function applySuggestion() {
		if (!suggestion) return;
		applyingSuggestion = true;
		error = null;
		try {
			const updated = await settingsApi.update({ flat_speed_kmh: suggestion.suggested_kmh });
			apply(updated);
			saved = true;
			// The rider's own current speed just changed to the suggested one -
			// re-fetching would only echo the same value back as "suggested".
			suggestion = null;
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to apply the suggestion';
		} finally {
			applyingSuggestion = false;
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

			{#if suggestion}
				<div class="suggestion">
					<p>
						Your rides suggest {suggestion.suggested_kmh} km/h (from {suggestion.sample_size} rides).
						Apply?
					</p>
					<button type="button" onclick={applySuggestion} disabled={applyingSuggestion}>
						{applyingSuggestion ? 'Applying…' : 'Apply'}
					</button>
				</div>
			{/if}

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

		<p class="hint">Moovelo {appVersion}</p>
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
	.hint {
		color: var(--text-muted);
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
		color: var(--text);
	}
	input {
		font: inherit;
		font-size: 1rem;
		padding: 0.45rem 0.6rem;
		border: 1px solid var(--input-border);
		border-radius: 6px;
		background: var(--input-bg);
		color: var(--text);
	}
	.suggestion {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.8rem;
		padding: 0.6rem 0.8rem;
		border: 1px solid var(--input-border);
		border-radius: 6px;
		background: var(--surface);
	}
	.suggestion p {
		margin: 0;
		font-size: 0.85rem;
		color: var(--text);
	}
	.suggestion button {
		flex-shrink: 0;
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
		/* Inverted CTA - always contrasts with the page, so background/colour
		   are the --text/--bg pair rather than --surface: swapping under
		   dark mode is the point (dark-on-light becomes light-on-dark), not
		   an oversight. */
		background: var(--text);
		color: var(--bg);
		cursor: pointer;
	}
	button:disabled {
		opacity: 0.6;
		cursor: default;
	}
	.saved {
		color: var(--success-text);
		font-size: 0.9rem;
	}
	.error {
		color: var(--danger-text);
		font-size: 0.9rem;
	}
</style>
