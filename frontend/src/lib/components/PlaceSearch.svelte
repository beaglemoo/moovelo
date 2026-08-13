<script lang="ts">
	import { places, type PlaceResult } from '$lib/api';
	import { shortLength } from '$lib/format';
	import { units } from '$lib/units.svelte';

	interface Props {
		/** Map centre, so a search for "Newport" prefers the one you are
		 * looking at. Many English places share a name. */
		near: { lat: number; lon: number } | null;
		onPick: (place: PlaceResult, action: 'from' | 'add' | 'to') => void;
	}

	let { near, onPick }: Props = $props();

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

	let timer: ReturnType<typeof setTimeout> | null = null;
	let inflight: AbortController | null = null;
	let input: HTMLInputElement | undefined = $state();

	function reset() {
		// Abort as well as clear: without this, dismissing the list with
		// Escape or a click outside leaves the request running, and when it
		// lands `open = true` reopens the dropdown the rider just closed.
		inflight?.abort();
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
		timer = setTimeout(() => void run(term), DEBOUNCE_MS);
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
			if (!controller.signal.aborted) searching = false;
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
		if (open && !(event.target as HTMLElement)?.closest('.search')) reset();
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
		border: 1px solid #ccc;
		border-radius: 4px;
		background: #fffffff0;
		font: inherit;
		font-size: 0.9rem;
	}

	input:focus {
		outline: 2px solid #268bd2;
		outline-offset: -1px;
	}

	.spinner {
		position: absolute;
		right: 10px;
		top: 6px;
		color: #93a1a1;
	}

	.results {
		position: absolute;
		top: calc(100% + 4px);
		left: 0;
		right: 0;
		margin: 0;
		padding: 4px;
		list-style: none;
		background: #fff;
		border: 1px solid #ddd;
		border-radius: 4px;
		box-shadow: 0 4px 14px rgba(0, 0, 0, 0.18);
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
		background: #eee8d5;
	}

	.empty {
		padding: 8px 10px;
		color: #93a1a1;
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
		color: #073642;
	}

	.meta {
		font-size: 0.75rem;
		color: #586e75;
	}

	.actions {
		display: flex;
		gap: 2px;
		padding-right: 4px;
	}

	.actions button {
		padding: 3px 7px;
		font-size: 0.75rem;
		border: 1px solid #ddd;
		border-radius: 3px;
		background: #fff;
		color: #586e75;
		cursor: pointer;
	}

	.actions button:hover {
		border-color: #268bd2;
		color: #268bd2;
	}
</style>
