<script lang="ts">
	import { places, type Waypoint } from '$lib/api';
	import { SvelteMap } from 'svelte/reactivity';

	interface Props {
		waypoints: Waypoint[];
		/** Gates reverse-geocoding entirely - false when the place index does
		 * not exist, in which case every row falls back to its positional
		 * label. */
		searchEnabled: boolean;
		hoveredIndex: number | null;
		onHover: (index: number | null) => void;
		onReorder: (from: number, to: number) => void;
		onRemove: (index: number) => void;
	}

	let { waypoints, searchEnabled, hoveredIndex, onHover, onReorder, onRemove }: Props = $props();

	function key(wp: Waypoint): string {
		return `${wp.lat.toFixed(5)},${wp.lon.toFixed(5)}`;
	}

	// Resolved names, keyed by rounded coordinate rather than array index -
	// an unmoved waypoint is never re-fetched even as reordering moves it to
	// a different index. SvelteMap (not a plain Map) so a landed fetch
	// repaints its row without a separate $state mirror.
	const nameCache = new SvelteMap<string, string | null>();

	// No generation guard, deliberately: a result is keyed by its own
	// coordinates, so it can never be stale for that key - a reorder just
	// re-renders rows against the same cache. The earlier generation-counter
	// version dropped any in-flight result the moment the array changed
	// (even a reorder of identical points), while its already-written null
	// placeholder blocked every retry - the row was stuck positional
	// forever. In-flight keys live in `pending` instead of a cache
	// placeholder, so a dropped fetch never poisons future attempts.
	// A plain Set, not SvelteSet: the $effect below both reads and writes
	// it, so reactivity would make the effect re-trigger itself on every
	// fetch start and completion. Rows repaint via nameCache alone.
	// eslint-disable-next-line svelte/prefer-svelte-reactivity
	const pending = new Set<string>();

	$effect(() => {
		const wps = waypoints;
		if (!searchEnabled) return;
		for (const wp of wps) {
			const k = key(wp);
			if (nameCache.has(k) || pending.has(k)) continue;
			pending.add(k);
			places.reverse(wp.lat, wp.lon).then((result) => {
				pending.delete(k);
				nameCache.set(k, result?.name ?? null);
			});
		}
	});

	function positional(index: number, count: number): string {
		if (index === 0) return 'Start';
		if (index === count - 1) return 'Finish';
		return `Via ${index}`;
	}

	// The positional role is always shown, even once a name resolves, so
	// that reordering ("move Finish up") reads meaningfully - a plain place
	// name alone would not say which slot in the route it now occupies.
	function label(wp: Waypoint, index: number, count: number): string {
		const role = positional(index, count);
		const name = nameCache.get(key(wp));
		return name ? `${role} - ${name}` : role;
	}

	function dotColor(index: number, count: number): string {
		if (index === 0) return '#2aa198';
		if (index === count - 1) return '#dc322f';
		return '#268bd2';
	}

	// Native HTML5 drag-and-drop, component-local state only - `from` is the
	// row that started the drag, `over` is purely a visual insertion cue.
	let dragFrom: number | null = $state(null);
	let dragOver: number | null = $state(null);

	function onDragStart(event: DragEvent, index: number) {
		dragFrom = index;
		if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move';
	}

	function onDragOver(event: DragEvent, index: number) {
		event.preventDefault();
		dragOver = index;
	}

	function onDrop(event: DragEvent, index: number) {
		event.preventDefault();
		if (dragFrom !== null && dragFrom !== index) onReorder(dragFrom, index);
		dragFrom = null;
		dragOver = null;
	}

	function onDragEnd() {
		dragFrom = null;
		dragOver = null;
	}
</script>

<div class="waypoints">
	<h3>Waypoints</h3>
	<!-- Fixed height whatever the state, matching ClimbsList/PoiPanel's
	     .results: the panel below the map must not grow under the rider's
	     pointer. -->
	<div class="results">
		<ul>
			{#each waypoints as wp, i (i)}
				<li
					class:hovered={hoveredIndex === i}
					class:drag-over={dragOver === i && dragFrom !== null && dragFrom !== i}
					draggable="true"
					ondragstart={(e) => onDragStart(e, i)}
					ondragover={(e) => onDragOver(e, i)}
					ondrop={(e) => onDrop(e, i)}
					ondragend={onDragEnd}
					onmouseenter={() => onHover(i)}
					onmouseleave={() => onHover(null)}
					onfocusin={() => onHover(i)}
					onfocusout={() => onHover(null)}
				>
					<span class="handle" aria-hidden="true">⠿</span>
					<span class="dot" style="background: {dotColor(i, waypoints.length)}"></span>
					<span class="what">{label(wp, i, waypoints.length)}</span>
					<!-- Up/down are the accessible AND touch path: HTML5 drag-and-drop
					     fires no events at all on touch, and there is no keyboard
					     equivalent for it, so these are not a fallback for the drag
					     above - they are the only way a large slice of riders can
					     reorder at all. -->
					<button
						type="button"
						aria-label="Move waypoint {i + 1} up"
						disabled={i === 0}
						onclick={() => onReorder(i, i - 1)}
					>
						&uarr;
					</button>
					<button
						type="button"
						aria-label="Move waypoint {i + 1} down"
						disabled={i === waypoints.length - 1}
						onclick={() => onReorder(i, i + 1)}
					>
						&darr;
					</button>
					<button type="button" aria-label="Remove waypoint {i + 1}" onclick={() => onRemove(i)}>
						&times;
					</button>
				</li>
			{/each}
		</ul>
	</div>
</div>

<style>
	.waypoints {
		display: flex;
		flex-direction: column;
		min-height: 0;
	}
	h3 {
		margin: 0;
		padding: 0.2rem 0.2rem 0.3rem;
		font-size: 0.85rem;
		color: var(--text-muted);
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
		align-items: center;
		gap: 6px;
		padding: 3px 6px;
		border-radius: 3px;
		font-size: 0.82rem;
	}
	li.hovered {
		background: var(--surface-sunken);
	}
	li.drag-over {
		box-shadow: inset 0 2px 0 var(--accent);
	}
	.handle {
		flex: none;
		color: var(--text-muted);
		cursor: grab;
	}
	.dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		flex: none;
	}
	.what {
		flex: 1;
		min-width: 0;
		color: var(--text);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	li button {
		flex: none;
		border: 0;
		background: none;
		font: inherit;
		color: var(--text-muted);
		cursor: pointer;
		padding: 1px 4px;
		border-radius: 3px;
	}
	li button:hover:not(:disabled) {
		background: var(--surface-sunken);
	}
	li button:disabled {
		color: var(--text-muted);
		cursor: default;
	}
</style>
