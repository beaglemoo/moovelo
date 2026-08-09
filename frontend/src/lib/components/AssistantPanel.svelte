<script lang="ts">
	import { onDestroy } from 'svelte';
	import {
		streamAssistantChat,
		type AssistantHandle,
		type AssistantProposal,
		type BicycleCostingOptions,
		type Preset,
		type Waypoint
	} from '$lib/api';

	interface Props {
		/** What the planner is showing, so "from here" and "the current
		 * finish" resolve to real coordinates the model never sees. */
		waypoints: Waypoint[];
		centre: Waypoint | null;
		preset: Preset;
		costingOptions: BicycleCostingOptions | null;
		/** Places the assistant looked up, with coordinates - for dropping
		 * pins. Wired to the map in a later change. */
		onHandles?: (handles: AssistantHandle[]) => void;
		/** Raised when the assistant has built a route. The planner owns it
		 * from there - it draws the preview and decides when it has gone
		 * stale - and hands it back below for the card to render. */
		onProposal?: (proposal: AssistantProposal) => void;
		/** The offer currently on the table, or null. */
		proposal?: AssistantProposal | null;
		onAccept?: () => void;
		onDiscard?: () => void;
	}

	let {
		waypoints,
		centre,
		preset,
		costingOptions,
		onHandles,
		onProposal,
		proposal = null,
		onAccept,
		onDiscard
	}: Props = $props();

	interface Entry {
		role: 'user' | 'assistant';
		text: string;
	}

	let entries = $state<Entry[]>([]);
	let draft = $state('');
	let busy = $state(false);
	let status = $state('');
	let error = $state<string | null>(null);
	let log = $state<HTMLDivElement | null>(null);
	// Refs minted in earlier turns. The server keeps no conversation state, so
	// carrying these is what lets turn three still resolve "place:1".
	let knownHandles: AssistantHandle[] = [];
	let controller: AbortController | null = null;

	const TOOL_LABELS: Record<string, string> = {
		search_place: 'Searching places',
		find_pois: 'Looking for places to stop',
		plan_route: 'Planning the route',
		generate_loop: 'Building a loop',
		route_stats: 'Measuring the route',
		modify_route: 'Adjusting the route'
	};

	/** The model writes markdown whatever the prompt says. Rendering it would
	 * mean a sanitiser and a parser for two features nobody asked for, so the
	 * markers are stripped and the text shown as prose. */
	function stripMarkdown(text: string): string {
		return text
			.replace(/^#{1,6}\s+/gm, '')
			.replace(/\*\*(.+?)\*\*/g, '$1')
			.replace(/(^|\s)\*(\S(?:.*?\S)?)\*(?=\s|$)/g, '$1$2')
			.replace(/`([^`]+)`/g, '$1');
	}

	function scrollToEnd() {
		// After the DOM has the new text, or it scrolls to the old height.
		requestAnimationFrame(() => log?.scrollTo({ top: log.scrollHeight }));
	}

	async function send() {
		const question = draft.trim();
		if (!question || busy) return;
		draft = '';
		error = null;
		entries = [...entries, { role: 'user', text: question }, { role: 'assistant', text: '' }];
		scrollToEnd();

		busy = true;
		status = 'Thinking';
		controller = new AbortController();
		const history = entries
			.filter((entry) => entry.text || entry.role === 'user')
			.map((entry) => ({ role: entry.role, content: entry.text }));

		try {
			const stream = streamAssistantChat(
				{
					messages: history,
					waypoints,
					centre,
					preset,
					costing_options: costingOptions,
					known_handles: knownHandles
				},
				controller.signal
			);
			for await (const event of stream) {
				switch (event.type) {
					case 'token':
						appendToReply(event.text);
						break;
					case 'tool_call':
						status = TOOL_LABELS[event.name] ?? 'Working';
						break;
					case 'tool_result':
						if (event.error) status = `${TOOL_LABELS[event.name] ?? 'That step'} did not work`;
						break;
					case 'handles':
						knownHandles = event.handles;
						onHandles?.(event.handles);
						break;
					case 'proposal':
						onProposal?.(event.proposal);
						break;
					case 'error':
						error = event.message;
						break;
					case 'done':
						status = '';
						break;
				}
			}
		} catch (caught) {
			// An abort is the rider pressing Stop, not a failure to report.
			if (!(caught instanceof DOMException && caught.name === 'AbortError')) {
				error = caught instanceof Error ? caught.message : 'The assistant failed';
			}
		} finally {
			busy = false;
			status = '';
			controller = null;
			// An aborted or failed turn can leave the placeholder empty; drop it
			// rather than showing a blank bubble.
			if (entries.at(-1)?.role === 'assistant' && !entries.at(-1)?.text) {
				entries = entries.slice(0, -1);
			}
		}
	}

	function appendToReply(text: string) {
		const last = entries.at(-1);
		if (!last || last.role !== 'assistant') return;
		entries = [...entries.slice(0, -1), { role: 'assistant', text: last.text + text }];
		scrollToEnd();
	}

	function stop() {
		controller?.abort();
	}

	// A stream outlives its component otherwise: nothing stops it, and nothing
	// can - the Stop button went with the panel. The backend keeps paying a
	// model to finish a turn with no reader.
	onDestroy(() => controller?.abort());

	function onKeydown(event: KeyboardEvent) {
		if (event.key === 'Enter' && !event.shiftKey) {
			event.preventDefault();
			send();
		}
	}
</script>

<div class="assistant">
	<div class="assistant-head">Route assistant</div>
	<div class="assistant-log" bind:this={log} role="log" aria-label="Assistant conversation">
		{#if entries.length === 0}
			<p class="assistant-hint">
				Ask for a route - "a 40 km gravel loop from here, with water on it". Whatever it comes up
				with becomes ordinary waypoints you can drag.
			</p>
		{/if}
		{#each entries as entry, index (index)}
			<p class="assistant-entry" class:mine={entry.role === 'user'}>
				{stripMarkdown(entry.text)}
			</p>
		{/each}
		{#if status}
			<p class="assistant-status">{status}…</p>
		{/if}
		{#if error}
			<p class="assistant-error">{error}</p>
		{/if}
	</div>
	{#if proposal}
		<div class="assistant-proposal">
			<!-- Every figure here is read off the snapshot Valhalla returned,
			     never off the model's prose - the prompt forbids inventing
			     numbers, but this is what makes it structurally impossible for
			     an invented one to reach the rider as a measurement. -->
			<p class="assistant-proposal-stats">
				{(proposal.snapshot.distance_m / 1000).toFixed(1)} km, ↗ {Math.round(
					proposal.snapshot.ascent_m
				)} m
			</p>
			<div class="assistant-proposal-buttons">
				<button type="button" class="primary" onclick={() => onAccept?.()}>Use this route</button>
				<button type="button" onclick={() => onDiscard?.()}>Discard</button>
			</div>
		</div>
	{/if}
	<div class="assistant-ask">
		<input
			type="text"
			bind:value={draft}
			onkeydown={onKeydown}
			placeholder="Ask for a route"
			aria-label="Ask the route assistant"
			disabled={busy}
		/>
		{#if busy}
			<button type="button" onclick={stop}>Stop</button>
		{:else}
			<button type="button" onclick={send} disabled={!draft.trim()}>Ask</button>
		{/if}
	</div>
</div>

<style>
	.assistant {
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}
	.assistant-head {
		font-size: 0.8rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: #93a1a1;
	}
	.assistant-log {
		height: 150px;
		overflow-y: auto;
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
		padding-right: 0.3rem;
		font-size: 0.85rem;
	}
	.assistant-hint {
		margin: 0;
		color: #93a1a1;
	}
	.assistant-entry {
		margin: 0;
		white-space: pre-wrap;
		color: #073642;
	}
	.assistant-entry.mine {
		color: #586e75;
		font-weight: 600;
	}
	.assistant-status {
		margin: 0;
		color: #93a1a1;
		font-style: italic;
	}
	.assistant-error {
		margin: 0;
		color: #dc322f;
	}
	.assistant-proposal {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
		padding: 0.4rem 0.5rem;
		border: 1px solid #859900;
		border-radius: 6px;
		background: #8599001a;
	}
	.assistant-proposal-stats {
		margin: 0;
		font-size: 0.85rem;
		color: #073642;
	}
	.assistant-proposal-buttons {
		display: flex;
		gap: 0.4rem;
	}
	.assistant-proposal-buttons button {
		font: inherit;
		font-size: 0.8rem;
		padding: 0.25rem 0.6rem;
		border: 1px solid #ccc;
		border-radius: 6px;
		background: #fff;
		cursor: pointer;
	}
	.assistant-proposal-buttons .primary {
		background: #859900;
		border-color: #859900;
		color: #fff;
	}
	.assistant-ask {
		display: flex;
		gap: 0.4rem;
	}
	.assistant-ask input {
		flex: 1;
		min-width: 0;
		font: inherit;
		font-size: 0.85rem;
		padding: 0.35rem 0.5rem;
		border: 1px solid #ccc;
		border-radius: 6px;
	}
	.assistant-ask button {
		font: inherit;
		font-size: 0.85rem;
		padding: 0.35rem 0.8rem;
		border: 1px solid #ccc;
		border-radius: 6px;
		background: #fff;
		cursor: pointer;
	}
	.assistant-ask button:disabled {
		opacity: 0.5;
		cursor: default;
	}
</style>
