<script lang="ts">
	import { places, type PlaceResult } from '$lib/api';
	import { shortLength } from '$lib/format';
	import { units } from '$lib/units.svelte';

	interface Props {
		/** Map centre, so a search for "Newport" prefers the one you are
		 * looking at. Many English places share a name. */
		near: { lat: number; lon: number } | null;
		onPick: (place: PlaceResult, action: 'from' | 'add' | 'to') => void;
		/** Lets the planner yield its guide while this absolute dropdown is
		 * open, rather than placing another card over the result actions. */
		onOpenChange?: (open: boolean) => void;
	}

	let { near, onPick, onOpenChange }: Props = $props();

	// Two characters is the server's minimum too; sending one would only
	// earn a 422.
	const MIN_QUERY = 2;
	const DEBOUNCE_MS = 250;

	let query = $state('');
	let results: PlaceResult[] = $state([]);
	let open = $state(false);
	let active = $state(-1);
	let searching = $state(false);
	let failed = $state(false);

	$effect(() => onOpenChange?.(open));

	let timer: ReturnType<typeof setTimeout> | null = null;
	let inflight: AbortController | null = null;
	let input: HTMLInputElement | undefined = $state();

	function reset() {
		// Cancel both stages. A click outside can arrive before the debounce
		// has fired, when there is no open list or request to abort yet; leaving
		// that timer alive would open an abandoned search a moment later.
		if (timer) clearTimeout(timer);
		timer = null;
		inflight?.abort();
		inflight = null;
		searching = false;
		results = [];
		active = -1;
		open = false;
		failed = false;
	}

	function onInput() {
		if (timer) clearTimeout(timer);
		const term = query.trim();
		if (term.length < MIN_QUERY) {
			inflight?.abort();
			searching = false;
			reset();
			return;
		}
		// Supersede the active request as soon as the query changes. Waiting
		// for the next debounce to fire would leave the old response free to
		// render under the new input for up to DEBOUNCE_MS.
		inflight?.abort();
		inflight = null;
		searching = false;
		// A completed result belongs to the text that produced it. Clear it
		// immediately while the replacement debounce is pending so Enter (or a
		// fast tap) cannot choose stale coordinates under the new query.
		results = [];
		active = -1;
		open = false;
		failed = false;
		timer = setTimeout(() => {
			timer = null;
			void run(term);
		}, DEBOUNCE_MS);
	}

	async function run(term: string) {
		// Every keystroke supersedes the last, so the previous request is
		// abandoned rather than left to land out of order.
		inflight?.abort();
		const controller = new AbortController();
		inflight = controller;
		searching = true;
		try {
			const found = await places.search(term, near ?? undefined, controller.signal);
			if (controller.signal.aborted) return;
			results = found;
			active = found.length > 0 ? 0 : -1;
			open = true;
			failed = false;
		} catch (err) {
			if (controller.signal.aborted || (err instanceof DOMException && err.name === 'AbortError'))
				return;
			results = [];
			active = -1;
			open = true;
			failed = true;
		} finally {
			if (inflight === controller) {
				searching = false;
				inflight = null;
			}
		}
	}

	function choose(place: PlaceResult, action: 'from' | 'add' | 'to') {
		onPick(place, action);
		query = '';
		reset();
		input?.blur();
	}

	function onKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape') {
			reset();
			return;
		}
		if (!open || results.length === 0) return;

		if (event.key === 'ArrowDown') {
			event.preventDefault();
			active = (active + 1) % results.length;
		} else if (event.key === 'ArrowUp') {
			event.preventDefault();
			active = (active - 1 + results.length) % results.length;
		} else if (event.key === 'Enter' && active >= 0) {
			event.preventDefault();
			// Enter adds a waypoint, matching what clicking the map does.
			choose(results[active], 'add');
		}
	}
</script>

<div class="search">
	<input
		bind:this={input}
		bind:value={query}
		oninput={onInput}
		onkeydown={onKeydown}
		onfocus={() => {
			if (results.length > 0) open = true;
		}}
		type="search"
		role="combobox"
		aria-expanded={open}
		aria-controls="place-results"
		aria-autocomplete="list"
		aria-activedescendant={active >= 0 ? `place-${results[active].id}` : undefined}
		placeholder="Search for a place"
		autocomplete="off"
	/>
	{#if searching}
		<span class="spinner" aria-hidden="true">…</span>
	{/if}

	{#if open}
		<ul class="results" id="place-results" role="listbox" aria-label="Places">
			{#if failed}
				<li class="empty" role="presentation">Search failed. Try again.</li>
			{:else if results.length === 0}
				<li class="empty" role="presentation">Nothing found</li>
			{:else}
				{#each results as place, index (place.id)}
					<li
						id="place-{place.id}"
						role="option"
						aria-selected={index === active}
						class:active={index === active}
					>
						<button
							type="button"
							class="name"
							onmouseenter={() => (active = index)}
							onclick={() => choose(place, 'add')}
						>
							<span class="label">{place.name}</span>
							<span class="meta"
								>{place.place_type}{place.distance_m !== null
									? ` · ${shortLength(place.distance_m, units.system)}`
									: ''}</span
							>
						</button>
						<span class="actions">
							<button type="button" onclick={() => choose(place, 'from')} title="Route from here"
								>From</button
							>
							<button type="button" onclick={() => choose(place, 'to')} title="Route to here"
								>To</button
							>
						</span>
					</li>
				{/each}
			{/if}
		</ul>
	{/if}
</div>

<svelte:window
	onclick={(event) => {
		if (!(event.target as HTMLElement)?.closest('.search')) reset();
	}}
/>

<style>
	.search {
		position: relative;
		width: min(360px, 70vw);
	}

	input {
		width: 100%;
		box-sizing: border-box;
		padding: 7px 10px;
		border: 1px solid var(--input-border);
		border-radius: 4px;
		background: var(--surface);
		color: var(--text);
		font: inherit;
		font-size: 0.9rem;
	}

	input:focus {
		outline: 2px solid var(--accent);
		outline-offset: -1px;
	}

	.spinner {
		position: absolute;
		right: 10px;
		top: 6px;
		color: var(--text-muted);
	}

	.results {
		position: absolute;
		top: calc(100% + 4px);
		left: 0;
		right: 0;
		margin: 0;
		padding: 4px;
		list-style: none;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 4px;
		box-shadow: 0 4px 14px var(--shadow);
		max-height: 320px;
		overflow-y: auto;
	}

	.results li {
		display: flex;
		align-items: center;
		gap: 4px;
		border-radius: 3px;
	}

	.results li.active {
		background: var(--surface-sunken);
	}

	.empty {
		padding: 8px 10px;
		color: var(--text-muted);
		font-size: 0.85rem;
	}

	.name {
		flex: 1;
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 1px;
		padding: 6px 8px;
		background: none;
		border: 0;
		font: inherit;
		text-align: left;
		cursor: pointer;
	}

	.label {
		font-size: 0.9rem;
		color: var(--text);
	}

	.meta {
		font-size: 0.75rem;
		color: var(--text-muted);
	}

	.actions {
		display: flex;
		gap: 2px;
		padding-right: 4px;
	}

	.actions button {
		padding: 3px 7px;
		font-size: 0.75rem;
		border: 1px solid var(--border);
		border-radius: 3px;
		background: var(--surface);
		color: var(--text-muted);
		cursor: pointer;
	}

	.actions button:hover {
		border-color: var(--link);
		color: var(--link);
	}
</style>
