<script lang="ts">
	import { PAVED_SURFACES } from '$lib/surface';
	import type { SurfaceBreakdown } from '$lib/api';

	interface Props {
		surface: SurfaceBreakdown;
	}

	let { surface }: Props = $props();

	// Valhalla's surface enum, grouped into what a rider actually decides on:
	// can a road bike cope with this, or does it want a gravel/cross bike.
	// The paved group lives in $lib/surface so LoopPanel's paved percentage
	// can never drift from this bar's.
	const PAVED = PAVED_SURFACES;
	const GRAVEL = ['compacted', 'dirt', 'gravel'];
	const PATH = ['path'];

	function sum(keys: string[]): number {
		return keys.reduce((total, key) => total + (surface.surface_m[key] ?? 0), 0);
	}

	const groups = $derived.by(() => {
		const paved = sum(PAVED);
		const gravel = sum(GRAVEL);
		const path = sum(PATH);
		// impassable, unknown/undocumented surface strings - anything not
		// already counted above.
		const known = new Set([...PAVED, ...GRAVEL, ...PATH]);
		const other = Object.entries(surface.surface_m)
			.filter(([key]) => !known.has(key))
			.reduce((total, [, metres]) => total + metres, 0);
		return [
			{ key: 'paved', label: 'Paved', metres: paved, colour: '#268bd2' },
			{ key: 'gravel', label: 'Gravel', metres: gravel, colour: '#b58900' },
			{ key: 'path', label: 'Path', metres: path, colour: '#859900' },
			{ key: 'other', label: 'Other', metres: other, colour: '#93a1a1' }
		].filter((g) => g.metres > 0);
	});

	// The cycleway figure is deliberately not derived from use_m: surface mix
	// (what the ground is made of) and marked cycling infrastructure (whether
	// there is a dedicated lane) measure two different things, and mixing
	// them would misrepresent both.
	const cyclewayPercent = $derived(
		surface.total_m > 0 ? Math.round((surface.cycle_lane_m / surface.total_m) * 100) : 0
	);

	function percent(metres: number): number {
		return surface.total_m > 0 ? Math.round((metres / surface.total_m) * 100) : 0;
	}
</script>

<div class="surface">
	<div class="bar" role="img" aria-label="Surface breakdown">
		{#each groups as group (group.key)}
			<span
				class="segment"
				style="width: {percent(group.metres)}%; background: {group.colour}"
				title="{group.label} {percent(group.metres)}%"
			></span>
		{/each}
	</div>
	<div class="legend">
		{#each groups as group (group.key)}
			<span class="entry">
				<span class="dot" style="background: {group.colour}"></span>
				{group.label}
				{percent(group.metres)}%
			</span>
		{/each}
		<span class="cycleway">{cyclewayPercent}% on cycleway</span>
	</div>
</div>

<style>
	.surface {
		display: flex;
		flex-direction: column;
		gap: 4px;
		padding: 0.2rem 0.2rem 0.4rem;
	}
	.bar {
		display: flex;
		height: 8px;
		border-radius: 4px;
		overflow: hidden;
		background: #eee8d5;
	}
	.segment {
		height: 100%;
	}
	.legend {
		display: flex;
		flex-wrap: wrap;
		gap: 0.7rem;
		font-size: 0.78rem;
		color: #586e75;
	}
	.entry {
		display: flex;
		align-items: center;
		gap: 4px;
	}
	.dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		flex: none;
	}
	.cycleway {
		color: #93a1a1;
	}
</style>
