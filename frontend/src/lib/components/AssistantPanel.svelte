<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import {
		streamAssistantChat,
		type AssistantHandle,
		type AssistantProposal,
		type BicycleCostingOptions,
		type Preset,
		type Waypoint
	} from '$lib/api';
	import { distance, elevation } from '$lib/format';
	import { units } from '$lib/units.svelte';

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
		// Prose the model produced in a round that ended in a tool call -
		// "let me search for that", "now I will plan it". The panel already
		// says what each tool is doing (TOOL_LABELS drives `status`), so
		// rendering this as well showed the rider the same progress twice and
		// stacked every round's narration into one bubble ahead of the answer.
		//
		// Kept rather than dropped, for two reasons: it still goes back to the
		// model as conversation history, and it is the fallback if a turn puts
		// its only prose in a round that also called a tool.
		steps?: string[];
	}

	/** What a turn said, narration included - the model's own view of it. */
	function historyText(entry: Entry): string {
		return [...(entry.steps ?? []), entry.text].filter(Boolean).join(' ');
	}

	let entries = $state<Entry[]>([]);
	let draft = $state('');
	let busy = $state(false);
	let status = $state('');
	let error = $state<string | null>(null);
	// Why the last turn ended, when it did not end normally. A proposal built
	// during a turn that ran out of budget, timed out or got stuck is a
	// partial answer, and presenting it identically to a confident one invites
	// a one-click accept of a route the assistant itself hedged about.
	let incomplete = $state<string | null>(null);
	let log = $state<HTMLDivElement | null>(null);
	// Refs minted in earlier turns. The server keeps no conversation state, so
	// carrying these is what lets turn three still resolve "place:1".
	let knownHandles: AssistantHandle[] = [];
	let controller: AbortController | null = null;

	// The floating card, following the basemap-toggle precedent
	// (BASEMAP_STORAGE_KEY, MapView.svelte) rather than inventing a new one.
	// Collapsed to a pill by default: with nothing else able to render
	// without a route, this used to be the only thing in the docked panel,
	// which meant a full-width band across the bottom of the map before any
	// work had even started.
	const STORAGE_KEY = 'moovelo:assistant-box';
	// How much of the header must stay reachable after a clamp - enough to
	// grab it again, not the whole card.
	const HEADER_MARGIN = 48;

	let root = $state<HTMLDivElement | undefined>();
	let collapsed = $state(true);
	// Anchored bottom-right (right/bottom in the stylesheet) until the rider
	// drags it once; from then on it is explicit left/top, and the two
	// anchors are never mixed - see boxStyle.
	let left = $state<number | null>(null);
	let top = $state<number | null>(null);

	function persistBox() {
		localStorage.setItem(STORAGE_KEY, JSON.stringify({ collapsed, left, top }));
	}

	function toggleCollapsed() {
		collapsed = !collapsed;
		persistBox();
	}

	// Keeps the card fully inside .map-area (its positioning parent) with at
	// least HEADER_MARGIN px reachable, so a window dragged off-screen and
	// then resized narrower cannot strand itself out of reach. Run on drop
	// and on window resize - not continuously during a drag.
	function clampPosition() {
		if (!root || left === null || top === null) return;
		const area = root.parentElement;
		if (!area) return;
		const areaRect = area.getBoundingClientRect();
		const boxRect = root.getBoundingClientRect();
		const minLeft = HEADER_MARGIN - boxRect.width;
		const maxLeft = Math.max(minLeft, areaRect.width - HEADER_MARGIN);
		const maxTop = Math.max(0, areaRect.height - HEADER_MARGIN);
		left = Math.min(Math.max(left, minLeft), maxLeft);
		top = Math.min(Math.max(top, 0), maxTop);
	}

	const boxStyle = $derived(
		left === null || top === null
			? 'right: 10px; bottom: 34px; left: auto; top: auto;'
			: `left: ${left}px; top: ${top}px; right: auto; bottom: auto;`
	);

	// Pointer Events with setPointerCapture, not mouse/touch listeners on
	// window: one code path covers mouse, touch and pen, and capture
	// guarantees the matching pointerup lands on this element even if the
	// pointer leaves it mid-drag - dropping over the toolbar or the zoom
	// controls must not lose the gesture the way an earlier map drag once
	// did (see the comment on the route-line drag in MapView.svelte).
	function onHeaderPointerDown(event: PointerEvent) {
		if (event.button !== 0) return;
		const header = event.currentTarget as HTMLElement;
		// The close button lives inside the header so it can sit flush beside
		// the label. Without this check, setPointerCapture below grabs every
		// pointerdown in the header - including one that started on the
		// button - and the button's own click, which fires after capture is
		// released, gets retargeted to the header instead. That made the
		// close button unclickable by mouse or touch; only Tab+Enter worked,
		// because keyboard activation never goes through pointer capture.
		if ((event.target as HTMLElement | null)?.closest('button')) return;
		if (!root) return;
		const area = root.parentElement;
		if (!area) return;

		// A header drag must never reach the map underneath: stop it here
		// rather than relying on it simply not bubbling to the right target.
		event.preventDefault();
		event.stopPropagation();

		const areaRect = area.getBoundingClientRect();
		const boxRect = root.getBoundingClientRect();
		// Switches anchoring to explicit left/top on the first drag, replacing
		// whichever corner the card started pinned to.
		left = boxRect.left - areaRect.left;
		top = boxRect.top - areaRect.top;
		const originLeft = left;
		const originTop = top;
		const startX = event.clientX;
		const startY = event.clientY;

		header.setPointerCapture(event.pointerId);

		const onMove = (moveEvent: PointerEvent) => {
			left = originLeft + (moveEvent.clientX - startX);
			top = originTop + (moveEvent.clientY - startY);
		};
		const onUp = (upEvent: PointerEvent) => {
			header.releasePointerCapture(upEvent.pointerId);
			header.removeEventListener('pointermove', onMove);
			header.removeEventListener('pointerup', onUp);
			header.removeEventListener('pointercancel', onUp);
			clampPosition();
			persistBox();
		};
		header.addEventListener('pointermove', onMove);
		header.addEventListener('pointerup', onUp);
		header.addEventListener('pointercancel', onUp);
	}

	onMount(() => {
		try {
			const raw = localStorage.getItem(STORAGE_KEY);
			if (raw) {
				const stored = JSON.parse(raw) as { collapsed?: unknown; left?: unknown; top?: unknown };
				collapsed = stored.collapsed !== false;
				if (typeof stored.left === 'number' && typeof stored.top === 'number') {
					left = stored.left;
					top = stored.top;
				}
			}
		} catch {
			// A corrupted or foreign value - fall back to the default corner
			// rather than throwing.
		}
		// A position saved on a wider window must not strand the card off
		// this one.
		clampPosition();
	});

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
		incomplete = null;
		controller = new AbortController();
		const history = entries
			.map((entry) => ({ role: entry.role, content: historyText(entry) }))
			.filter((entry) => entry.content || entry.role === 'user');

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
						// Every delta of this round arrived before this frame, so
						// whatever is in the bubble now is narration, not the answer.
						setAside();
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
						incomplete = event.stoppedEarly;
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
			// On every exit path, not just the clean one: a turn can end - done,
			// aborted, failed - with its prose sitting in `steps` because the
			// round that produced it also called a tool. Narration is a poor
			// answer, but it is the answer the model gave, and the alternative
			// here is the blank bubble the next branch then deletes.
			const last = entries.at(-1);
			if (last?.role === 'assistant' && !last.text && last.steps?.length) {
				entries = [
					...entries.slice(0, -1),
					{ ...last, text: last.steps[last.steps.length - 1], steps: last.steps.slice(0, -1) }
				];
			}
			// An aborted or failed turn can leave the placeholder empty; drop it
			// rather than showing a blank bubble.
			if (entries.at(-1)?.role === 'assistant' && !entries.at(-1)?.text) {
				entries = entries.slice(0, -1);
			}
		}
	}

	/** Move the current round's prose out of the bubble and into the trail. */
	function setAside() {
		const last = entries.at(-1);
		if (!last || last.role !== 'assistant' || !last.text.trim()) return;
		entries = [
			...entries.slice(0, -1),
			{ ...last, text: '', steps: [...(last.steps ?? []), last.text.trim()] }
		];
	}

	function appendToReply(text: string) {
		const last = entries.at(-1);
		if (!last || last.role !== 'assistant') return;
		// Spread, not a fresh literal: `steps` holds the narration set aside by
		// earlier rounds of this same turn, and rebuilding the entry dropped it.
		entries = [...entries.slice(0, -1), { ...last, text: last.text + text }];
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

<svelte:window onresize={clampPosition} />

<div bind:this={root} class="assistant" class:collapsed style={boxStyle}>
	{#if collapsed}
		<button
			type="button"
			class="assistant-pill"
			onclick={toggleCollapsed}
			aria-label="Ask for a route"
		>
			<span class="assistant-pill-long" aria-hidden="true">Ask for a route</span>
			<span class="assistant-pill-short" aria-hidden="true">Ask</span>
		</button>
	{:else}
		<!-- Drag by the header only: the body holds selectable text (the log,
		     the input), and dragging from there would fight that selection.
		     Not an interactive role - the collapse button beside it is the
		     accessible control; this div is a pointer-only drag handle. -->
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div class="assistant-head" onpointerdown={onHeaderPointerDown}>
			<span>Route assistant</span>
			<button
				type="button"
				class="assistant-close"
				onclick={toggleCollapsed}
				aria-label="Collapse the assistant"
			>
				×
			</button>
		</div>
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
			<div class="assistant-proposal" class:partial={incomplete}>
				{#if incomplete}
					<p class="assistant-proposal-warning">
						This is as far as I got - check it before using it.
					</p>
				{/if}
				<!-- Every figure here is read off the snapshot Valhalla returned,
				     never off the model's prose - the prompt forbids inventing
				     numbers, but this is what makes it structurally impossible for
				     an invented one to reach the rider as a measurement. -->
				<p class="assistant-proposal-stats">
					{distance(proposal.snapshot.distance_m, units.system)}, ↗ {elevation(
						proposal.snapshot.ascent_m,
						units.system
					)}
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
	{/if}
</div>

<style>
	/* Bottom-right is the only corner nothing else claims: the left column is
	   the toolbar, search bar, avoid chips and planner guide top to bottom;
	   top-right is the loop/alternates cards and MapLibre's own zoom stack;
	   bottom-left is the basemap switch and cycle-route toggle; bottom-centre
	   is the error banner. z-index 8 sits above the toolbar tier (5-6) and below the
	   context menu and preset popover (10) and the save dialog (20). */
	.assistant {
		position: absolute;
		z-index: 8;
		display: flex;
		flex-direction: column;
	}
	/* Collapsed by default. There is no backdrop-filter or translucent card
	   anywhere else in this codebase - every other card is opaque var(--surface)
	   - so this is a deliberate new pattern, not an extension of one, and it
	   needs an opaque fallback for a browser that does not support the filter:
	   unreadable text over a busy map is the failure mode otherwise.
	   color-mix keeps the translucency theme-aware (--surface is already
	   light/dark); a browser without color-mix falls back to the @supports
	   block's plain var(--surface), same as the no-backdrop-filter case. */
	.assistant-pill {
		font: inherit;
		font-size: 0.85rem;
		padding: 0.5rem 1rem;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: color-mix(in srgb, var(--surface) 90%, transparent);
		backdrop-filter: blur(8px);
		box-shadow: 0 8px 30px var(--shadow);
		color: var(--text);
		cursor: pointer;
	}
	.assistant-pill-short {
		display: none;
	}
	@supports not (backdrop-filter: blur(1px)) {
		.assistant-pill {
			background: var(--surface);
		}
	}
	.assistant:not(.collapsed) {
		width: min(22rem, 90vw);
		max-height: min(60%, 26rem);
		gap: 0.4rem;
		padding: 0.5rem 0.6rem 0.6rem;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: color-mix(in srgb, var(--surface) 90%, transparent);
		backdrop-filter: blur(8px);
		box-shadow: 0 8px 30px var(--shadow);
		overflow: hidden;
	}
	@supports not (backdrop-filter: blur(1px)) {
		.assistant:not(.collapsed) {
			background: var(--surface);
		}
	}
	/* The collapsed assistant is a small edge control on every mobile layout,
	   not a full-width band across the route. The 72px bottom anchor clears the
	   basemap/overlay row while keeping the centre of the map readable. Desktop
	   retains the full label and the rider's saved drag position. */
	@media (max-width: 900px) {
		.assistant.collapsed {
			left: auto !important;
			right: 10px !important;
			top: auto !important;
			bottom: 72px !important;
			width: auto !important;
		}
		.assistant-pill {
			min-width: 44px;
			min-height: 44px;
			padding: 0.5rem 0.75rem;
		}
		.assistant-pill-long {
			display: none;
		}
		.assistant-pill-short {
			display: inline;
		}
	}
	/* An expanded conversation needs the phone width. !important overrides a
	   position dragged on desktop so that saved coordinates cannot strand it. */
	@media (max-width: 760px) {
		.assistant:not(.collapsed) {
			left: 10px !important;
			right: 10px !important;
			top: auto !important;
			bottom: 72px !important;
			width: auto !important;
		}
	}
	.assistant-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.4rem;
		font-size: 0.8rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
		cursor: grab;
		touch-action: none;
		user-select: none;
	}
	.assistant-head:active {
		cursor: grabbing;
	}
	.assistant-close {
		border: none;
		background: transparent;
		padding: 0.1rem 0.2rem;
		font-size: 0.9rem;
		line-height: 1;
		color: var(--text-muted);
		cursor: pointer;
	}
	.assistant-log {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
		padding-right: 0.3rem;
		font-size: 0.85rem;
	}
	.assistant-hint {
		margin: 0;
		color: var(--text-muted);
	}
	.assistant-entry {
		margin: 0;
		white-space: pre-wrap;
		color: var(--text);
	}
	.assistant-entry.mine {
		color: var(--text-muted);
		font-weight: 600;
	}
	.assistant-status {
		margin: 0;
		color: var(--text-muted);
		font-style: italic;
	}
	.assistant-error {
		margin: 0;
		color: var(--danger-text);
	}
	.assistant-proposal.partial {
		border-color: var(--warning);
		/* Accent alpha-tint highlight fill - deliberately left literal. */
		background: #b589001a;
		flex-wrap: wrap;
	}
	.assistant-proposal-warning {
		margin: 0;
		width: 100%;
		font-size: 0.8rem;
		/* Readable body text: --warning on the pale warning tint is only ~3.2:1.
		   The warning cue is carried by the card's --warning border and tint. */
		color: var(--text);
	}
	.assistant-proposal {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
		padding: 0.4rem 0.5rem;
		border: 1px solid var(--success);
		border-radius: 6px;
		/* Accent alpha-tint highlight fill - deliberately left literal. */
		background: #8599001a;
	}
	.assistant-proposal-stats {
		margin: 0;
		font-size: 0.85rem;
		color: var(--text);
	}
	.assistant-proposal-buttons {
		display: flex;
		gap: 0.4rem;
	}
	.assistant-proposal-buttons button {
		font: inherit;
		font-size: 0.8rem;
		padding: 0.25rem 0.6rem;
		border: 1px solid var(--input-border);
		border-radius: 6px;
		background: var(--surface);
		color: var(--text);
		cursor: pointer;
	}
	.assistant-proposal-buttons .primary {
		background: var(--success);
		border-color: var(--success);
		/* Black, not white: --success is a fixed light green (both themes) on
		   which white text is only 3.2:1; black clears AA at 6.5:1. */
		color: #000000;
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
		border: 1px solid var(--input-border);
		background: var(--input-bg);
		color: var(--text);
		border-radius: 6px;
	}
	.assistant-ask button {
		font: inherit;
		font-size: 0.85rem;
		padding: 0.35rem 0.8rem;
		border: 1px solid var(--input-border);
		border-radius: 6px;
		background: var(--surface);
		color: var(--text);
		cursor: pointer;
	}
	.assistant-ask button:disabled {
		opacity: 0.5;
		cursor: default;
	}
</style>
