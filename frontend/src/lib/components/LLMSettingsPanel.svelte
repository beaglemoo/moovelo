<script lang="ts">
	import {
		llmAdmin,
		type LLMModelInfo,
		type LLMProbeResult,
		type LLMProviderInfo,
		type LLMSettings,
		type LLMSettingsPatch
	} from '$lib/api';

	const OPENROUTER = 'https://openrouter.ai/api/v1';

	const SORTS: { value: string; label: string; hint: string }[] = [
		{ value: 'balanced', label: 'Balanced', hint: "The gateway's own choice of price and speed" },
		{ value: 'price', label: 'Cheapest', hint: 'Always route to the lowest-priced provider' },
		{ value: 'throughput', label: 'Fastest', hint: 'Highest tokens per second' },
		{ value: 'latency', label: 'Lowest latency', hint: 'Quickest to first token' }
	];

	let settings: LLMSettings | null = $state(null);
	let models: LLMModelInfo[] = $state([]);
	let providers: LLMProviderInfo[] = $state([]);
	let error: string | null = $state(null);
	let saving = $state(false);
	let probing = $state(false);
	let probe: LLMProbeResult | null = $state(null);
	let modelsLoading = $state(false);
	let modelFilter = $state('');

	// Form state, seeded from the server and edited locally.
	let baseUrl = $state('');
	let model = $state('');
	let apiKey = $state('');
	let sort = $state('balanced');
	let providerOrder = $state('');
	let maxPrice: number | null = $state(null);

	const isOpenRouter = $derived(baseUrl.includes('openrouter.ai'));
	const filteredModels = $derived(
		modelFilter.trim()
			? models.filter((m) => m.id.toLowerCase().includes(modelFilter.trim().toLowerCase()))
			: models
	);
	const orderedProviders = $derived(
		providerOrder
			.split(',')
			.map((p) => p.trim())
			.filter(Boolean)
	);

	// What the fields were seeded with, so save() can tell an edited field from
	// one merely showing the env-resolved default. Without this, every save
	// pinned the current env value into the database as an explicit override
	// and a later change to LLM_BASE_URL was silently ignored for ever after.
	let seeded = {
		baseUrl: '',
		model: '',
		sort: 'balanced',
		providerOrder: '',
		maxPrice: null as number | null
	};

	function seed(s: LLMSettings) {
		settings = s;
		baseUrl = s.base_url ?? s.effective_base_url;
		model = s.model ?? s.effective_model;
		sort = s.provider_sort ?? 'balanced';
		providerOrder = s.provider_order ?? '';
		maxPrice = s.max_prompt_price;
		apiKey = '';
		seeded = { baseUrl, model, sort, providerOrder, maxPrice };
	}

	async function load() {
		try {
			seed(await llmAdmin.get());
		} catch (err) {
			error = err instanceof Error ? err.message : 'Could not load assistant settings';
		}
	}
	load();

	async function loadModels() {
		modelsLoading = true;
		error = null;
		try {
			models = await llmAdmin.models(true);
		} catch (err) {
			error = err instanceof Error ? err.message : 'Could not list models';
		} finally {
			modelsLoading = false;
		}
	}

	async function loadProviders() {
		if (!model) return;
		try {
			providers = await llmAdmin.providers(model);
		} catch {
			// A local endpoint has no provider concept; an empty table is the
			// honest answer rather than an error the operator cannot act on.
			providers = [];
		}
	}

	async function save() {
		saving = true;
		error = null;
		probe = null;
		try {
			// Only changed fields are sent. The backend applies exclude_unset,
			// so an omitted field keeps whatever it had - which for a field
			// that was only ever showing the env default means it stays an env
			// default rather than being frozen into the database.
			const patch: LLMSettingsPatch = {};
			if (baseUrl !== seeded.baseUrl) patch.base_url = baseUrl;
			if (model !== seeded.model) patch.model = model;
			if (providerOrder !== seeded.providerOrder) patch.provider_order = providerOrder;
			if (sort !== seeded.sort) patch.provider_sort = sort;
			if (maxPrice !== seeded.maxPrice) patch.max_prompt_price = maxPrice;
			// Only send the key when one was typed - an untouched field must
			// never clear a stored key.
			if (apiKey) patch.api_key = apiKey;
			seed(await llmAdmin.update(patch));
		} catch (err) {
			error = err instanceof Error ? err.message : 'Save failed';
		} finally {
			saving = false;
		}
	}

	async function clearKey() {
		if (!confirm('Remove the stored API key?')) return;
		try {
			seed(await llmAdmin.update({ api_key: '' }));
		} catch (err) {
			error = err instanceof Error ? err.message : 'Could not clear the key';
		}
	}

	async function runProbe() {
		probing = true;
		probe = null;
		error = null;
		try {
			probe = await llmAdmin.test();
		} catch (err) {
			error = err instanceof Error ? err.message : 'Test failed';
		} finally {
			probing = false;
		}
	}

	function toggleProvider(name: string) {
		const current = orderedProviders;
		providerOrder = current.includes(name)
			? current.filter((p) => p !== name).join(', ')
			: [...current, name].join(', ');
	}

	function price(v: number | null): string {
		return v === null ? '-' : `$${v.toFixed(2)}/M`;
	}
</script>

<section class="llm">
	<h2>Route assistant</h2>
	<p class="intro">
		An optional AI planner. Off unless an endpoint and model are set. Your route coordinates and
		place names are sent to whatever endpoint you configure, so a local model keeps everything on
		your network.
	</p>

	{#if error}
		<p class="error">{error}</p>
	{/if}

	{#if settings}
		<p class="status" class:on={settings.enabled}>
			{settings.enabled ? 'Enabled' : 'Disabled'}
			{#if settings.enabled}
				- using <code>{settings.effective_model}</code>
			{/if}
			{#if settings.env_configured}
				<span class="from-env"
					>Environment variables are set; anything left blank falls back to them.</span
				>
			{/if}
		</p>

		<div class="field">
			<span class="label">Endpoint</span>
			<div class="modes">
				<button type="button" class:active={isOpenRouter} onclick={() => (baseUrl = OPENROUTER)}
					>OpenRouter</button
				>
				<button
					type="button"
					class:active={!isOpenRouter}
					onclick={() => {
						if (isOpenRouter) baseUrl = '';
					}}>Local or custom</button
				>
			</div>
			<input
				type="url"
				bind:value={baseUrl}
				placeholder="http://your-ollama-host:11434/v1"
				aria-label="Endpoint base URL"
			/>
		</div>

		<div class="field">
			<label for="llm-key">API key</label>
			<div class="row">
				<input
					id="llm-key"
					type="password"
					bind:value={apiKey}
					placeholder={settings.api_key_set ? 'Stored - type to replace' : 'Not set'}
					autocomplete="off"
				/>
				{#if settings.api_key_set}
					<button type="button" onclick={clearKey}>Clear</button>
				{/if}
			</div>
			<span class="hint">A local endpoint usually needs none. Stored in the database.</span>
		</div>

		<div class="field">
			<label for="llm-model">Model</label>
			<div class="row">
				<input
					id="llm-model"
					type="text"
					bind:value={model}
					placeholder="openai/gpt-5.6-luna-pro"
				/>
				<button type="button" onclick={loadModels} disabled={modelsLoading}>
					{modelsLoading ? 'Loading...' : 'Browse'}
				</button>
			</div>
			<span class="hint">
				Must support tool calling, or the assistant can answer questions but never plan a route.
			</span>
			{#if models.length > 0}
				<input
					type="search"
					bind:value={modelFilter}
					placeholder="Filter {models.length} tool-capable models"
					aria-label="Filter models"
				/>
				<ul class="models">
					{#each filteredModels.slice(0, 60) as m (m.id)}
						<li>
							<button type="button" class:picked={m.id === model} onclick={() => (model = m.id)}>
								<span class="id">{m.id}</span>
								<span class="cost"
									>{price(m.prompt_price)} in / {price(m.completion_price)} out</span
								>
							</button>
						</li>
					{/each}
				</ul>
			{/if}
		</div>

		{#if isOpenRouter}
			<div class="field">
				<label for="llm-sort">Routing</label>
				<select id="llm-sort" bind:value={sort}>
					{#each SORTS as s (s.value)}
						<option value={s.value}>{s.label}</option>
					{/each}
				</select>
				<span class="hint">{SORTS.find((s) => s.value === sort)?.hint}</span>
			</div>

			<div class="field">
				<label for="llm-max-price">Maximum input price</label>
				<div class="row">
					<input
						id="llm-max-price"
						type="number"
						min="0"
						step="0.05"
						bind:value={maxPrice}
						placeholder="no limit"
					/>
					<span class="unit">$ per million tokens</span>
				</div>
				<span class="hint">
					The only setting that caps what a request can cost. The same model is often served by
					providers an order of magnitude apart in price.
				</span>
			</div>

			<div class="field">
				<span class="label">Preferred providers</span>
				<div class="row">
					<button type="button" onclick={loadProviders} disabled={!model}>
						Show providers for this model
					</button>
					{#if providerOrder}
						<button type="button" onclick={() => (providerOrder = '')}>Clear</button>
					{/if}
				</div>
				{#if providers.length > 0}
					<ul class="providers">
						{#each providers as p, i (p.name + i)}
							<li>
								<button
									type="button"
									class:picked={orderedProviders.includes(p.name)}
									onclick={() => toggleProvider(p.name)}
								>
									<span class="id">{p.name}</span>
									<span class="cost"
										>{price(p.prompt_price)} in / {price(p.completion_price)} out</span
									>
								</button>
							</li>
						{/each}
					</ul>
				{/if}
				<span class="hint">
					A preference, not a restriction - the gateway still falls back to another provider rather
					than failing a reply.
				</span>
			</div>
		{/if}

		<div class="actions">
			<button type="button" class="primary" onclick={save} disabled={saving}>
				{saving ? 'Saving...' : 'Save'}
			</button>
			<button type="button" onclick={runProbe} disabled={probing || !settings.enabled}>
				{probing ? 'Testing...' : 'Test'}
			</button>
		</div>

		{#if probe}
			<div class="probe" class:bad={!probe.ok}>
				<strong>{probe.ok ? 'Working' : 'Not usable'}</strong>
				<ul>
					<li>Replied in {probe.latency_s}s</li>
					{#if probe.provider}<li>Served by {probe.provider}</li>{/if}
					{#if probe.cost !== null}<li>Cost ${probe.cost.toFixed(6)}</li>{/if}
					<li>Called a tool: {probe.tool_called ? 'yes' : 'no'}</li>
				</ul>
				<p>{probe.detail}</p>
			</div>
		{/if}
	{/if}
</section>

<style>
	.llm {
		margin-top: 2rem;
		max-width: 40rem;
	}
	.intro,
	.hint {
		color: var(--text-muted);
		font-size: 0.85rem;
	}
	.hint {
		display: block;
		margin-top: 0.2rem;
	}
	.status {
		font-size: 0.9rem;
		color: var(--text-muted);
	}
	.status.on {
		color: var(--link);
	}
	.from-env {
		display: block;
		color: var(--text-muted);
		font-size: 0.8rem;
	}
	.field {
		margin: 1rem 0;
	}
	.field label,
	.field .label {
		display: block;
		font-weight: 600;
		font-size: 0.85rem;
		margin-bottom: 0.3rem;
		color: var(--text);
	}
	.row {
		display: flex;
		gap: 0.5rem;
		align-items: center;
	}
	input,
	select {
		/* flex:1 only bites inside .row; the width keeps a standalone input
		   from collapsing to its content. */
		flex: 1;
		width: 100%;
		box-sizing: border-box;
		padding: 0.4rem 0.5rem;
		border: 1px solid var(--input-border);
		background: var(--input-bg);
		color: var(--text);
		border-radius: 4px;
		font-size: 0.9rem;
		min-width: 0;
	}
	select {
		width: auto;
		min-width: 12rem;
	}
	input[type='search'] {
		margin-top: 0.4rem;
	}
	.unit {
		font-size: 0.8rem;
		color: var(--text-muted);
		white-space: nowrap;
	}
	button {
		padding: 0.4rem 0.7rem;
		border: 1px solid var(--input-border);
		border-radius: 4px;
		background: var(--surface);
		color: var(--text);
		cursor: pointer;
		font-size: 0.85rem;
	}
	button:disabled {
		opacity: 0.5;
		cursor: default;
	}
	button.primary {
		background: var(--accent-fill);
		border-color: var(--accent);
		color: #fff;
	}
	.modes {
		display: flex;
		gap: 0.4rem;
		margin-bottom: 0.4rem;
	}
	.modes button.active {
		background: var(--surface-sunken);
		border-color: var(--text-muted);
	}
	.models,
	.providers {
		list-style: none;
		margin: 0.4rem 0 0;
		padding: 0;
		max-height: 180px;
		overflow-y: auto;
		border: 1px solid var(--border);
		border-radius: 4px;
	}
	.models li button,
	.providers li button {
		display: flex;
		justify-content: space-between;
		gap: 1rem;
		width: 100%;
		border: 0;
		border-radius: 0;
		background: none;
		text-align: left;
		padding: 0.3rem 0.5rem;
	}
	.models li button.picked,
	.providers li button.picked {
		/* Mark the selection with an inset accent bar rather than an accent-tinted
		   fill: a light-enough tint to be visible in dark dropped the row's own
		   muted price text below WCAG AA, so keep the darker sunken fill (text
		   stays readable) and let the bar carry the selected state. */
		background: var(--surface-sunken);
		box-shadow: inset 3px 0 0 var(--accent);
		font-weight: 600;
	}
	.id {
		font-family: ui-monospace, monospace;
		font-size: 0.8rem;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.cost {
		color: var(--text-muted);
		font-size: 0.78rem;
		white-space: nowrap;
	}
	.actions {
		display: flex;
		gap: 0.5rem;
		margin-top: 1.2rem;
	}
	.probe {
		margin-top: 0.8rem;
		padding: 0.6rem 0.8rem;
		border: 1px solid var(--accent);
		border-radius: 6px;
		font-size: 0.85rem;
	}
	.probe.bad {
		border-color: var(--danger);
	}
	.probe ul {
		margin: 0.3rem 0;
		padding-left: 1.1rem;
	}
	.probe p {
		margin: 0.3rem 0 0;
		color: var(--text-muted);
	}
	.error {
		color: var(--danger-text);
		font-size: 0.9rem;
	}
</style>
