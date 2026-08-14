<script lang="ts">
	import type { WindSegment } from '$lib/api';
	import { distance, speed } from '$lib/format';
	import { units } from '$lib/units.svelte';

	interface Props {
		startTime: string;
		onStartTimeChange: (value: string) => void;
		loading: boolean;
		error: string | null;
		segments: WindSegment[];
		truncated: boolean;
		onShowWind: () => void;
	}

	let { startTime, onStartTimeChange, loading, error, segments, truncated, onShowWind }: Props =
		$props();

	function hhmm(iso: string): string {
		return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
	}

	function kmh(metresPerSecond: number): number {
		return metresPerSecond * 3.6;
	}

	// Wind direction is where it blows FROM; the arrow points where it blows
	// TOWARD, so it reads as "the wind is pushing this way".
	function arrowRotation(directionDeg: number): number {
		return (directionDeg + 180) % 360;
	}

	// Below this, the along-route push/drag is small enough that "headwind"
	// or "tailwind" would overstate it - it is mostly hitting the rider from
	// the side.
	const CROSSWIND_THRESHOLD_KMH = 3;

	function windLabel(headwindMs: number): { text: string; class: string } {
		const speedKmh = Math.abs(kmh(headwindMs));
		if (speedKmh < CROSSWIND_THRESHOLD_KMH) return { text: 'crosswind', class: 'cross' };
		const shown = speed(speedKmh, units.system);
		if (headwindMs > 0) return { text: `${shown} headwind`, class: 'head' };
		return { text: `${shown} tailwind`, class: 'tail' };
	}
</script>

<div class="weather">
	<div class="controls">
		<input
			type="datetime-local"
			value={startTime}
			onchange={(e) => onStartTimeChange((e.target as HTMLInputElement).value)}
		/>
		<!-- Fires only on this click - never from a route or preset change -
		     wind is the one call in the app that reaches an external service. -->
		<button type="button" onclick={onShowWind} disabled={loading}>
			{loading ? 'Checking…' : 'Show wind'}
		</button>
	</div>

	<div class="results">
		{#if error}
			<p class="note error">{error}</p>
		{:else if truncated}
			<p class="note">Start time is beyond the forecast window.</p>
		{:else if segments.length === 0}
			<p class="note">Pick a start time and press "Show wind".</p>
		{:else}
			<ul>
				{#each segments as segment, i (i)}
					{@const label = windLabel(segment.headwind_ms)}
					<li>
						<span class="at">{distance(segment.dist_along_m, units.system)}</span>
						<span class="time">{hhmm(segment.arrival_iso)}</span>
						<span
							class="arrow"
							style="transform: rotate({arrowRotation(segment.wind_direction_deg)}deg)"
							title="{Math.round(segment.wind_direction_deg)}° - blowing toward {Math.round(
								arrowRotation(segment.wind_direction_deg)
							)}°"
						>
							↑
						</span>
						<span class="speed">{speed(kmh(segment.wind_speed_ms), units.system)}</span>
						<span class="label {label.class}">{label.text}</span>
					</li>
				{/each}
			</ul>
		{/if}
	</div>
</div>

<style>
	.weather {
		display: flex;
		flex-direction: column;
		min-height: 0;
	}
	.controls {
		display: flex;
		gap: 6px;
		padding: 0.2rem 0.2rem 0.4rem;
	}
	.controls input {
		font: inherit;
		font-size: 0.8rem;
		border: 1px solid var(--input-border);
		background: var(--input-bg);
		color: var(--text);
		border-radius: 4px;
		padding: 2px 6px;
	}
	.controls button {
		border: 1px solid var(--accent-fill);
		background: var(--accent-fill);
		color: #fff;
		border-radius: 4px;
		padding: 2px 10px;
		font: inherit;
		font-size: 0.8rem;
		cursor: pointer;
	}
	.controls button:disabled {
		opacity: 0.6;
		cursor: default;
	}
	.results {
		height: 150px;
		overflow-y: auto;
	}
	ul {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	li {
		display: flex;
		align-items: baseline;
		gap: 8px;
		padding: 3px 6px;
		font-size: 0.82rem;
	}
	.at {
		color: var(--text-muted);
		font-variant-numeric: tabular-nums;
		flex: none;
		width: 4.2em;
	}
	.time {
		color: var(--text);
		font-variant-numeric: tabular-nums;
		flex: none;
		width: 3.4em;
	}
	.arrow {
		display: inline-block;
		flex: none;
		width: 1.2em;
		text-align: center;
	}
	.speed {
		color: var(--text);
		flex: none;
		width: 4.5em;
	}
	.label {
		flex: 1;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.label.head {
		color: var(--danger-text);
	}
	.label.tail {
		color: var(--success);
	}
	.label.cross {
		color: var(--text-muted);
	}
	.note {
		margin: 0;
		padding: 4px 6px 6px;
		color: var(--text-muted);
		font-size: 0.8rem;
	}
	.note.error {
		color: var(--danger-text);
	}
</style>
