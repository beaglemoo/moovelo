<script lang="ts">
	import type { Preset } from '$lib/api';

	interface Props {
		preset: Preset;
		onChange: (preset: Preset) => void;
		/** True when custom costing sliders (rather than one of the three
		 * named bundles) are what actually routed. Picking a named preset
		 * below always clears this. */
		customActive: boolean;
		onCustomize: () => void;
	}

	let { preset, onChange, customActive, onCustomize }: Props = $props();

	const PRESETS: { id: Preset; label: string; hint: string }[] = [
		{ id: 'road', label: 'Road', hint: 'Fast tarmac, avoids gravel' },
		{ id: 'gravel', label: 'Gravel', hint: 'Seeks unpaved tracks and towpaths' },
		{ id: 'quiet', label: 'Quiet', hint: 'Cycleways and calm streets' }
	];
</script>

<div class="presets" role="radiogroup" aria-label="Routing preset">
	{#each PRESETS as p (p.id)}
		<button
			type="button"
			role="radio"
			aria-checked={!customActive && preset === p.id}
			class:active={!customActive && preset === p.id}
			title={p.hint}
			onclick={() => onChange(p.id)}
		>
			{p.label}
		</button>
	{/each}
	<button
		type="button"
		role="radio"
		aria-checked={customActive}
		class:active={customActive}
		title="Fine-tune costing with sliders and save your own presets"
		onclick={onCustomize}
	>
		Custom…
	</button>
</div>

<style>
	.presets {
		display: flex;
		border-radius: 8px;
		overflow: hidden;
		border: 1px solid var(--input-border);
		background: var(--surface);
	}
	button {
		border: none;
		background: transparent;
		padding: 0.45rem 0.9rem;
		font: inherit;
		cursor: pointer;
		color: var(--text);
	}
	button + button {
		border-left: 1px solid var(--border);
	}
	button.active {
		/* Route-brand magenta, same literal MapView uses for the route line -
		   deliberately not tokenised, see step 3. */
		background: #d33682;
		color: #fff;
	}
</style>
