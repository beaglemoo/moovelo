<script lang="ts">
	import type { ElevationPoint } from '$lib/api';
	import { elevationRuns, GRADIENT_BANDS } from '$lib/gradient';

	interface Props {
		elevation: ElevationPoint[];
		onHover: (distM: number | null) => void;
		/** Set while a row in ClimbsList is hovered, so its distance range is
		 * highlighted here too - the first inbound hover into this chart; the
		 * outbound `onHover` above is untouched. */
		hoveredClimb?: { start_dist_m: number; end_dist_m: number } | null;
	}

	let { elevation, onHover, hoveredClimb = null }: Props = $props();

	const W = 800;
	const H = 150;
	const PAD_LEFT = 44;
	const PAD_RIGHT = 10;
	const PAD_TOP = 12;
	const PAD_BOTTOM = 22;

	let hover: ElevationPoint | null = $state(null);

	const totalDist = $derived(elevation.length ? elevation[elevation.length - 1].dist_m : 0);
	const minElev = $derived(Math.min(...elevation.map((p) => p.elev_m)));
	const maxElev = $derived(Math.max(...elevation.map((p) => p.elev_m)));
	const elevSpan = $derived(Math.max(maxElev - minElev, 20));

	function x(dist: number): number {
		return PAD_LEFT + (totalDist ? (dist / totalDist) * (W - PAD_LEFT - PAD_RIGHT) : 0);
	}

	function y(elev: number): number {
		return PAD_TOP + (1 - (elev - minElev) / elevSpan) * (H - PAD_TOP - PAD_BOTTOM);
	}

	const linePath = $derived(
		elevation
			.map((p, i) => `${i ? 'L' : 'M'}${x(p.dist_m).toFixed(1)},${y(p.elev_m).toFixed(1)}`)
			.join('')
	);
	const areaPath = $derived(
		linePath
			? `${linePath}L${x(totalDist).toFixed(1)},${H - PAD_BOTTOM}L${PAD_LEFT},${H - PAD_BOTTOM}Z`
			: ''
	);

	// Runs of consecutive elevation samples in the same gradient band, each
	// drawn as its own coloured stroke. Sharing the run's boundary sample
	// with its neighbour (endIdx of one run is startIdx of the next) keeps
	// the coloured line joined with no gaps.
	const gradientPaths = $derived.by(() => {
		return elevationRuns(elevation).map((run) => {
			const points = elevation.slice(run.startIdx, run.endIdx + 1);
			const path = points
				.map((p, i) => `${i ? 'L' : 'M'}${x(p.dist_m).toFixed(1)},${y(p.elev_m).toFixed(1)}`)
				.join('');
			return { path, band: run.band };
		});
	});

	// Only the bands this route actually has, in band order.
	const legendBands = $derived.by(() => {
		const present = new Set(gradientPaths.map((seg) => seg.band));
		return GRADIENT_BANDS.map((b, i) => ({ ...b, index: i })).filter((b) => present.has(b.index));
	});

	const gridLines = $derived.by(() => {
		const lines: { yPos: number; label: string }[] = [];
		if (!elevation.length) return lines;
		const step = elevSpan > 400 ? 200 : elevSpan > 150 ? 100 : elevSpan > 60 ? 50 : 20;
		for (let e = Math.ceil(minElev / step) * step; e <= maxElev; e += step) {
			lines.push({ yPos: y(e), label: `${e} m` });
		}
		return lines;
	});

	function handleMove(event: PointerEvent) {
		if (!elevation.length) return;
		const svg = event.currentTarget as SVGSVGElement;
		const rect = svg.getBoundingClientRect();
		const dist = ((event.clientX - rect.left) / rect.width) * W;
		const target = ((dist - PAD_LEFT) / (W - PAD_LEFT - PAD_RIGHT)) * totalDist;
		let best = elevation[0];
		for (const p of elevation) {
			if (Math.abs(p.dist_m - target) < Math.abs(best.dist_m - target)) best = p;
		}
		hover = best;
		onHover(best.dist_m);
	}

	function handleLeave() {
		hover = null;
		onHover(null);
	}

	function km(meters: number): string {
		return `${(meters / 1000).toFixed(1)} km`;
	}
</script>

{#if elevation.length > 1}
	<svg
		viewBox="0 0 {W} {H}"
		preserveAspectRatio="none"
		role="img"
		aria-label="Elevation profile"
		onpointermove={handleMove}
		onpointerleave={handleLeave}
	>
		{#each gridLines as line (line.label)}
			<line x1={PAD_LEFT} y1={line.yPos} x2={W - PAD_RIGHT} y2={line.yPos} class="grid" />
			<text x={PAD_LEFT - 6} y={line.yPos + 3} class="axis-label" text-anchor="end">
				{line.label}
			</text>
		{/each}
		{#if hoveredClimb}
			<rect
				x={x(hoveredClimb.start_dist_m)}
				y={PAD_TOP}
				width={Math.max(x(hoveredClimb.end_dist_m) - x(hoveredClimb.start_dist_m), 0)}
				height={H - PAD_TOP - PAD_BOTTOM}
				class="climb-highlight"
			/>
		{/if}
		<path d={areaPath} class="area" />
		{#each gradientPaths as segment, i (i)}
			<path d={segment.path} class="line" stroke={GRADIENT_BANDS[segment.band].colour} />
		{/each}
		{#if hover}
			<line
				x1={x(hover.dist_m)}
				y1={PAD_TOP}
				x2={x(hover.dist_m)}
				y2={H - PAD_BOTTOM}
				class="cursor"
			/>
			<text
				x={Math.min(Math.max(x(hover.dist_m), PAD_LEFT + 50), W - PAD_RIGHT - 50)}
				y={PAD_TOP + 2}
				class="hover-label"
				text-anchor="middle"
			>
				{km(hover.dist_m)} · {Math.round(hover.elev_m)} m
			</text>
		{/if}
		<text x={PAD_LEFT} y={H - 6} class="axis-label">0 km</text>
		<text x={W - PAD_RIGHT} y={H - 6} class="axis-label" text-anchor="end">{km(totalDist)}</text>
	</svg>
	<div class="legend">
		{#each legendBands as band (band.label)}
			<span class="legend-item">
				<span class="legend-dot" style="background: {band.colour}"></span>
				{band.label}
			</span>
		{/each}
	</div>
{/if}

<style>
	svg {
		display: block;
		width: 100%;
		height: 150px;
		touch-action: none;
	}
	.area {
		fill: #d3368222;
	}
	.climb-highlight {
		fill: #dc322f22;
	}
	.line {
		fill: none;
		stroke-width: 1.8;
		vector-effect: non-scaling-stroke;
	}
	.grid {
		stroke: #00000018;
		stroke-width: 1;
		vector-effect: non-scaling-stroke;
	}
	.cursor {
		stroke: #268bd2;
		stroke-width: 1.5;
		vector-effect: non-scaling-stroke;
	}
	.axis-label,
	.hover-label {
		font-size: 11px;
		fill: #586e75;
	}
	.hover-label {
		font-weight: 600;
		fill: #268bd2;
	}
	.legend {
		display: flex;
		flex-wrap: wrap;
		gap: 0.6rem;
		padding: 0.15rem 0.2rem 0.3rem;
	}
	.legend-item {
		display: inline-flex;
		align-items: center;
		gap: 0.3rem;
		font-size: 0.75rem;
		color: #586e75;
	}
	.legend-dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		flex-shrink: 0;
	}
</style>
