<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { ApiError, auth, type UserInfo } from '$lib/api';
	import { appUpdate } from '$lib/appUpdate.svelte';
	import { activityImportQueue, importQueue, pendingArchives } from '$lib/import.svelte';
	import { panelSize } from '$lib/panel.svelte';
	import { resetSession } from '$lib/session';
	import { theme } from '$lib/theme.svelte';
	import { units } from '$lib/units.svelte';
	import { unsaved } from '$lib/unsaved.svelte';
	import { onMount, type Snippet } from 'svelte';

	let { children }: { children: Snippet } = $props();

	let user: UserInfo | null = $state(null);
	let dropping = $state(false);
	let mobileMenuOpen = $state(false);
	let mobileMenuToggle: HTMLButtonElement | undefined = $state(undefined);
	// True while a log-out is tearing the session down. The confirm() has been
	// answered by then but auth.logout() is still in flight, so without this an
	// impatient re-click (likelier on a slow connection) re-enters logout() and
	// fires a second, spurious confirm for the one intent. Also disables the
	// button below.
	let loggingOut = $state(false);

	// Load the rider's metric/imperial and light/dark preferences once on the
	// client. onMount never runs during SSR, matching the other moovelo:*
	// localStorage reads. app.html's inline script already applied the theme
	// pre-hydration; this just brings the store's `mode` in sync with it.
	onMount(() => {
		units.load();
		theme.load();
		panelSize.load();
		void appUpdate.watch();
		// Capture outside clicks before MapLibre (or any page control) sees
		// them. The first tap dismisses the menu; it must not also add a
		// waypoint or activate the control underneath the overlay.
		window.addEventListener('click', handleWindowClick, true);
		return () => window.removeEventListener('click', handleWindowClick, true);
	});

	function themeLabel(mode: 'system' | 'light' | 'dark'): string {
		if (mode === 'light') return 'Light';
		if (mode === 'dark') return 'Dark';
		return 'Auto';
	}

	function closeMobileMenu() {
		mobileMenuOpen = false;
	}

	function closeMobileMenuAndRestoreFocus() {
		mobileMenuOpen = false;
		mobileMenuToggle?.focus();
	}

	function handleWindowClick(event: MouseEvent) {
		if (!mobileMenuOpen) return;
		const target = event.target;
		if (target instanceof Element && target.closest('.mobile-menu, .mobile-menu-toggle')) return;
		event.preventDefault();
		event.stopPropagation();
		closeMobileMenu();
	}

	function handleWindowKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape' && mobileMenuOpen) {
			event.preventDefault();
			closeMobileMenuAndRestoreFocus();
		}
	}

	// SvelteKit reuses this layout between pages. A menu opened on one page
	// must not remain over the next page after a client-side navigation.
	$effect(() => {
		void page.url.pathname;
		mobileMenuOpen = false;
	});

	// Files get dragged at the window, not at a particular drop target, so the
	// whole app accepts them once you are logged in.
	function hasFiles(event: DragEvent): boolean {
		return user !== null && (event.dataTransfer?.types.includes('Files') ?? false);
	}

	function onDragOver(event: DragEvent) {
		if (!hasFiles(event)) return;
		event.preventDefault();
		dropping = true;
	}

	function onDragLeave(event: DragEvent) {
		// relatedTarget is null when the pointer actually leaves the window,
		// rather than merely crossing between elements inside it.
		if (!event.relatedTarget) dropping = false;
	}

	async function onDrop(event: DragEvent) {
		if (!hasFiles(event)) return;
		event.preventDefault();
		dropping = false;
		const files = [...(event.dataTransfer?.files ?? [])];
		if (files.length === 0) return;

		// A file dropped on /activities is a ride, not a plan - that page has
		// its own import queue for exactly this, and forcing a navigation off
		// it used to abandon whatever it was doing (an in-flight archive
		// poll's status, in particular) with no way to recover it. Everywhere
		// else defaults to a planned route, including the planner itself,
		// which is what most dropped files actually are.
		if (page.url.pathname.startsWith('/activities')) {
			// Split the same way the page's own Import button does. A Strava
			// export is a .zip and goes to the archive endpoint; dropping one
			// used to be sent to the single-ride endpoint, which answers 400 -
			// so the flagship bulk import failed by drag and worked by button,
			// for the one file type that page advertises accepting.
			const zips = files.filter((file) => file.name.toLowerCase().endsWith('.zip'));
			const singles = files.filter((file) => !file.name.toLowerCase().endsWith('.zip'));
			if (singles.length) await activityImportQueue.add(singles);
			if (zips.length) pendingArchives.add(zips);
			return;
		}

		// Importing leaves the planner, so unsaved edits would vanish. The
		// planner's own beforeNavigate guard confirms that for every route out,
		// this one included, so it is not asked again here. If the rider
		// cancels it to keep editing, the navigation does not happen - so only
		// import once we have actually landed on the library, not behind them.
		await goto('/library');
		if (page.url.pathname !== '/library') return;
		await importQueue.add(files);
	}

	// On every navigation while logged out, try to resolve the session;
	// bounce to /login when there is none. Shared-route pages are public.
	$effect(() => {
		const path = page.url.pathname;
		if (user !== null || path.startsWith('/s/')) return;
		auth
			.me()
			.then((me) => (user = me))
			.catch((err) => {
				if (err instanceof ApiError && err.status === 401 && path !== '/login') {
					// The session is gone; bounce to login. Mark the teardown so
					// the planner's guard does not block this forced navigation, and
					// clear the session-scoped stores so an expired session does not
					// carry its history and import state into the next login.
					resetSession();
					unsaved.leaving = true;
					void goto('/login');
				}
			});
	});

	async function logout() {
		// A re-click while the previous log-out is still in flight is the same
		// intent, not a new one: drop it rather than run the confirm and teardown
		// a second time.
		if (loggingOut) return;
		// Confirm before destroying the session - a log-out with unsaved edits
		// still warrants a warning. Dismissing returns early with the session
		// and edits intact. On confirm, mark the teardown so the planner's guard
		// does not fire a second prompt on the goto below.
		if (
			unsaved.dirty &&
			!confirm('You have unsaved changes to the route you are planning.\n\nLog out and lose them?')
		) {
			return;
		}
		loggingOut = true;
		// Tear the session down first, and only mark the teardown once it has
		// actually happened. Setting unsaved.leaving before the awaited call let
		// a failed logout (a network error) leave the flag stuck true - it is
		// only ever reset when the planner re-mounts, which never happens
		// because the navigation to /login never ran - so the planner's guard
		// then silently skipped the rider's next real exit. On failure, stay put
		// with the session and guard intact.
		try {
			await auth.logout();
		} catch {
			// Failed teardown: session and guard intact, and the button must work
			// again so the rider can retry.
			loggingOut = false;
			return;
		}
		user = null;
		// Clear the planner history and import queues so the next account to log
		// in on this tab does not inherit this rider's undo stack or import
		// results - the singletons outlive a client-side logout otherwise.
		resetSession();
		unsaved.leaving = true;
		await goto('/login');
		// The root layout never unmounts, so reset for the next account that logs
		// in on this tab - otherwise its Log out button stays disabled.
		loggingOut = false;
	}
	/** The browser's own "leave site?" prompt when the planner is holding
	 * edits. Reloading or closing the tab discarded them silently, which is
	 * the one destructive action in the app with no confirmation - unlike
	 * importing, which already routes through `unsaved`. */
	function warnOnUnsaved(event: BeforeUnloadEvent) {
		if (!unsaved.dirty) return;
		event.preventDefault();
	}
</script>

<svelte:window
	ondragover={onDragOver}
	ondragleave={onDragLeave}
	ondrop={onDrop}
	onbeforeunload={warnOnUnsaved}
	onkeydown={handleWindowKeydown}
/>

<div class="shell">
	{#if user}
		<nav>
			<span class="brand">Moovelo</span>
			<div class="desktop-nav">
				<a href="/" class:active={page.url.pathname === '/'}>Planner</a>
				<a href="/library" class:active={page.url.pathname === '/library'}>Library</a>
				<a href="/activities" class:active={page.url.pathname === '/activities'}>Activities</a>
				<a href="/settings" class:active={page.url.pathname === '/settings'}>Settings</a>
				{#if user.is_admin}
					<a href="/admin" class:active={page.url.pathname === '/admin'}>Admin</a>
				{/if}
				<span class="spacer"></span>
				<button
					type="button"
					class="theme-toggle"
					onclick={() => theme.cycle()}
					title="Cycle theme: auto, light, dark"
					aria-label="Theme: {themeLabel(theme.mode)}"
				>
					{themeLabel(theme.mode)}
				</button>
				<button
					type="button"
					class="units-toggle"
					onclick={() => units.toggle()}
					title="Switch between metric and imperial units"
					aria-label="Units: {units.system === 'imperial' ? 'imperial' : 'metric'}"
				>
					{units.system === 'imperial' ? 'mi' : 'km'}
				</button>
				<span class="email">{user.email}</span>
				<button type="button" onclick={logout} disabled={loggingOut}>Log out</button>
			</div>
			<div class="nav-actions">
				{#if appUpdate.ready}
					<button
						type="button"
						class="app-update"
						onclick={() => appUpdate.apply()}
						disabled={appUpdate.applying}
					>
						{appUpdate.applying ? 'Updating' : 'Update'}
					</button>
				{/if}
				<button
					type="button"
					class="mobile-menu-toggle"
					aria-expanded={mobileMenuOpen}
					aria-controls="mobile-navigation"
					bind:this={mobileMenuToggle}
					onclick={() => (mobileMenuOpen = !mobileMenuOpen)}
				>
					Menu
				</button>
			</div>
			{#if mobileMenuOpen}
				<div class="mobile-menu" id="mobile-navigation">
					<div class="mobile-menu-account">{user.email}</div>
					<a href="/" class:active={page.url.pathname === '/'} onclick={closeMobileMenu}>Planner</a>
					<a
						href="/library"
						class:active={page.url.pathname === '/library'}
						onclick={closeMobileMenu}>Library</a
					>
					<a
						href="/activities"
						class:active={page.url.pathname === '/activities'}
						onclick={closeMobileMenu}>Activities</a
					>
					<a
						href="/settings"
						class:active={page.url.pathname === '/settings'}
						onclick={closeMobileMenu}>Settings</a
					>
					{#if user.is_admin}
						<a href="/admin" class:active={page.url.pathname === '/admin'} onclick={closeMobileMenu}
							>Admin</a
						>
					{/if}
					<div class="mobile-menu-preferences">
						<button type="button" onclick={() => theme.cycle()}>
							Theme: {themeLabel(theme.mode)}
						</button>
						<button type="button" onclick={() => units.toggle()}>
							Units: {units.system === 'imperial' ? 'mi' : 'km'}
						</button>
					</div>
					<button type="button" class="mobile-menu-logout" onclick={logout} disabled={loggingOut}
						>Log out</button
					>
				</div>
			{/if}
		</nav>
	{/if}
	<main class:planner={page.url.pathname === '/'}>
		{@render children()}
	</main>
	{#if dropping}
		<div class="dropzone">
			<p>Drop GPX, TCX or FIT files to import</p>
		</div>
	{/if}
</div>

<style>
	/* Semantic theme tokens. Light values are the app's original literals,
	   declared unconditionally on :root so a token always has a value even
	   before JS runs. Dark values are layered on in two ways that must stay
	   in sync: prefers-color-scheme for a rider who never touched the
	   toggle, and :root[data-theme] for a manual override - which has to
	   win over the media query in both directions, hence the
	   `:not([data-theme='light'])` guard on the media-query block. Nav
	   colours are NOT tokenised as light/dark - the nav is dark chrome in
	   both themes, so --nav-* is a single flat value, not swept.
	   Accent colours used as BACKGROUNDS (--accent/--danger/--success/
	   --warning) are Solarized's own accents and read correctly on both
	   grounds, so they are a single flat value. --link/--danger-text are the
	   TEXT roles for the same colours - flat --accent/--danger text fails
	   WCAG AA on the dark surface, so those two are swept per-theme like
	   --border/--border-strong/--input-border. Border tokens are brightened
	   in dark for the same reason: the original dark borders sat near
	   invisible (~1.3:1) against --surface/--bg. */
	:global(:root) {
		/* iOS draws a glass scrim across the top of an INSTALLED web app's
		   viewport and blurs whatever renders under it. Measured on the
		   reporter's iPhone with a ruler of text at 16px intervals: offsets 0
		   and 16 smeared, 32 and everything below sharp. It is absent in a
		   browser tab, where the browser's own chrome occupies that band, and
		   absent in landscape, where there is no status bar - hence the scope
		   below. One variable rather than a second set of numbers: the bar's
		   height and everything measured from it cannot then disagree. */
		--top-inset: 0px;
		/* Render native form controls (inputs, selects, scrollbars) and any
		   unstyled control in the theme's own palette - the belt-and-braces for
		   anything the tokens below do not explicitly paint. */
		color-scheme: light;
		--bg: #fdf6e3;
		--surface: #ffffff;
		--surface-sunken: #f2ede0;
		--nav-bg: #073642;
		--nav-text: #eee8d5;
		--nav-text-muted: #93a1a1;
		--nav-text-active: #fdf6e3;
		/* The compact header under 900px. Solarized map-paper in light, the
		   Solarized dark surface in dark - the bright strip above a dark app was
		   the reported complaint. Accents differ per theme because each is
		   picked for contrast against its OWN background: #1a6fb0 clears 3:1 on
		   the cream (2.94 for #268bd2 - it does not), and #268bd2 clears it on
		   the dark (2.44 for #1a6fb0 - it does not). The filled state keeps the
		   accent as its border, because that edge is the 3:1 boundary and not
		   decoration. */
		--mobile-nav-bg: #eee8d5;
		--mobile-nav-text: #073642;
		--mobile-nav-divider: #93a1a1;
		--mobile-nav-accent: #1a6fb0;
		--mobile-nav-accent-fill: #1a6fb0;
		--mobile-nav-accent-on-fill: #ffffff;
		--text: #073642;
		--text-muted: #586e75;
		--border: #ddd;
		--border-strong: #586e75;
		--input-bg: #ffffff;
		--input-border: #ccc;
		--shadow: rgba(0, 0, 0, 0.12);
		--overlay-scrim: rgba(7, 54, 66, 0.75);
		--accent: #268bd2;
		/* A darker blue for accent-filled buttons carrying white text: white on
		   --accent is only 3.68:1, but 5.32:1 on this. --accent itself stays the
		   brighter blue for borders/indicators (which pass the 3:1 UI bar). Flat
		   across themes - it is a fill with white text either way. */
		--accent-fill: #1a6fb0;
		--danger: #dc322f;
		--success: #859900;
		--warning: #b58900;
		--link: #1a6fb0;
		--danger-text: #d02c28;
		--success-text: #5f6e00;
		--warning-text: #7a5c00;
	}
	@media (prefers-color-scheme: dark) {
		:global(:root:not([data-theme='light'])) {
			color-scheme: dark;
			--bg: #002b36;
			--surface: #073642;
			--surface-sunken: #00212b;
			--text: #eee8d5;
			--text-muted: #93a1a1;
			--border: #4f8698;
			--border-strong: #5590a5;
			--mobile-nav-bg: #073642;
			--mobile-nav-text: #eee8d5;
			--mobile-nav-divider: #586e75;
			--mobile-nav-accent: #268bd2;
			--input-bg: #00212b;
			--input-border: #5590a5;
			--shadow: rgba(0, 0, 0, 0.45);
			--overlay-scrim: rgba(0, 0, 0, 0.62);
			--link: #4ea6e6;
			--danger-text: #ff6b64;
			--success-text: #9db600;
			--warning-text: #d9a400;
		}
	}
	:global(:root[data-theme='dark']) {
		color-scheme: dark;
		--mobile-nav-bg: #073642;
		--mobile-nav-text: #eee8d5;
		--mobile-nav-divider: #586e75;
		--mobile-nav-accent: #268bd2;
		--bg: #002b36;
		--surface: #073642;
		--surface-sunken: #00212b;
		--text: #eee8d5;
		--text-muted: #93a1a1;
		--border: #4f8698;
		--border-strong: #5590a5;
		--input-bg: #00212b;
		--input-border: #5590a5;
		--shadow: rgba(0, 0, 0, 0.45);
		--overlay-scrim: rgba(0, 0, 0, 0.62);
		--link: #4ea6e6;
		--danger-text: #ff6b64;
		--success-text: #9db600;
		--warning-text: #d9a400;
	}
	:global(:root[data-theme='light']) {
		color-scheme: light;
		--mobile-nav-bg: #eee8d5;
		--mobile-nav-text: #073642;
		--mobile-nav-divider: #93a1a1;
		--mobile-nav-accent: #1a6fb0;
		--bg: #fdf6e3;
		--surface: #ffffff;
		--surface-sunken: #f2ede0;
		--text: #073642;
		--text-muted: #586e75;
		--border: #ddd;
		--border-strong: #586e75;
		--input-bg: #ffffff;
		--input-border: #ccc;
		--shadow: rgba(0, 0, 0, 0.12);
		--overlay-scrim: rgba(7, 54, 66, 0.75);
		--link: #1a6fb0;
		--danger-text: #d02c28;
		--success-text: #5f6e00;
		--warning-text: #7a5c00;
	}
	:global(html, body) {
		margin: 0;
		height: 100%;
		width: 100%;
		overflow: hidden;
		overscroll-behavior: none;
		background: var(--bg);
		color: var(--text);
		/* An installed home-screen app runs in WKWebView, which applies text
		   autosizing; Safari on the same phone, same page, does not. WKWebView
		   inflates text per block by a non-integer factor, and a non-integer
		   glyph scale is what blurry text is - worst in a wide block like the
		   full-width nav row, barely visible in narrow controls like the map
		   toolbar pills. That difference (sharp in Safari, hazy in the installed
		   app) is the reported symptom, and this is the only knob that turns it
		   off. Both spellings: WebKit still only honours the prefixed one. */
		-webkit-text-size-adjust: 100%;
		text-size-adjust: 100%;
	}
	/* Unstyled native <select>s (filter dropdowns, the costing popover's bike
	   type) never had a colour literal to tokenise, so they fell back to the UA
	   palette - which color-scheme darkens but does not recolour to match the
	   surface. Theme them here; any component rule that styles its own select
	   still wins on specificity. */
	:global(select) {
		background: var(--surface);
		color: var(--text);
		border: 1px solid var(--input-border);
		border-radius: 6px;
	}
	.shell {
		display: flex;
		flex-direction: column;
		height: 100vh;
		height: 100dvh;
		overflow: hidden;
		font-family:
			system-ui,
			-apple-system,
			sans-serif;
	}
	nav {
		position: relative;
		z-index: 30;
		display: flex;
		align-items: center;
		gap: 1rem;
		padding: 0 0.9rem;
		height: 42px;
		background: var(--nav-bg);
		color: var(--nav-text);
		flex-shrink: 0;
		box-sizing: border-box;
		/* Every text box in this bar must land on a whole pixel. `align-items:
		   center` puts a child at (contentHeight - childHeight) / 2, so an odd
		   difference leaves it on a half pixel - which a 3x phone renders as
		   1.5 device pixels of smear, and reads as a blurry header while the
		   whole-pixel controls a few pixels below stay sharp. A fixed even
		   line-height (rather than `normal`, whose box is a fractional
		   font metric) is what makes the difference even, and integer font
		   sizes keep the baseline inside that box whole too. */
		line-height: 20px;
	}
	.desktop-nav {
		display: flex;
		align-items: center;
		gap: 1rem;
		min-width: 0;
		flex: 1;
	}
	.mobile-menu-toggle,
	.mobile-menu {
		display: none;
	}
	.nav-actions {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		margin-left: auto;
	}
	.app-update {
		white-space: nowrap;
	}
	.brand {
		font-weight: 700;
		margin-right: 0.5rem;
	}
	nav a {
		color: var(--nav-text-muted);
		text-decoration: none;
		font-size: 15px;
	}
	nav a.active,
	nav a:hover {
		color: var(--nav-text-active);
	}
	.spacer {
		flex: 1;
	}
	.email {
		font-size: 14px;
		color: var(--nav-text-muted);
	}
	nav button {
		border: 1px solid var(--border-strong);
		background: transparent;
		color: var(--nav-text);
		border-radius: 6px;
		padding: 0.25rem 0.7rem;
		font: inherit;
		font-size: 14px;
		line-height: 20px;
		cursor: pointer;
	}
	.theme-toggle {
		min-width: 3.2rem;
	}
	main {
		flex: 1;
		min-height: 0;
		overflow: auto;
		overscroll-behavior: contain;
	}
	main.planner {
		overflow: hidden;
		overscroll-behavior: none;
	}
	.dropzone {
		position: fixed;
		inset: 0;
		z-index: 50;
		display: grid;
		place-items: center;
		background: var(--overlay-scrim);
		/* Always-light text/border on the always-dark scrim, same reasoning as
		   the nav - not swept with the page's light/dark surface tokens. */
		color: #fdf6e3;
		font-size: 1.2rem;
		pointer-events: none;
	}
	.dropzone p {
		border: 2px dashed #93a1a1;
		border-radius: 10px;
		padding: 2rem 3rem;
	}
	/* The authenticated row can include five destinations, two preferences,
	   an email and Log out. It only fits honestly on a wide viewport; switching
	   before tablet width prevents a merely-long email from being clipped. */
	@media (display-mode: standalone) and (orientation: portrait) {
		:global(:root) {
			--top-inset: 32px;
		}
	}
	@media (max-width: 900px) {
		.desktop-nav {
			display: none;
		}
		nav {
			height: calc(44px + var(--top-inset));
			padding: var(--top-inset) 0.75rem 0;
			background: var(--mobile-nav-bg);
			color: var(--mobile-nav-text);
			/* The divider is an inset shadow, not a border. A 1px border would
			   take the content box to 43px, and `align-items: center` would then
			   put the 44px Menu button at top: -0.5 and the brand at top: 12.5 -
			   half-pixel text, which is 1.5 device pixels of smear on a 3x
			   phone and is exactly what made this bar read as hazy. A shadow
			   draws the same line without touching the box. */
			border-bottom: none;
			box-shadow: inset 0 -1px 0 var(--mobile-nav-divider);
			filter: none;
			backdrop-filter: none;
			-webkit-backdrop-filter: none;
		}
		.brand {
			margin-right: 0;
			color: var(--mobile-nav-text);
		}
		.app-update {
			min-height: 44px;
			padding-block: 0;
			background: var(--mobile-nav-accent-fill);
			border-color: var(--mobile-nav-accent);
			color: var(--mobile-nav-accent-on-fill);
			font-weight: 600;
		}
		.mobile-menu-toggle {
			display: block;
			min-height: 44px;
			min-width: 64px;
			padding-block: 0;
			background: transparent;
			border-color: var(--mobile-nav-accent);
			color: var(--mobile-nav-text);
			/* 600, not 650: an off-ramp weight has no matching face in most
			   stacks, and where it is synthesised the smearing is the very
			   artefact this block exists to remove. */
			font-weight: 600;
		}
		.mobile-menu-toggle:focus-visible {
			outline: 3px solid var(--mobile-nav-accent);
			outline-offset: -3px;
		}
		.mobile-menu-toggle[aria-expanded='true'] {
			background: var(--mobile-nav-accent-fill);
			border-color: var(--mobile-nav-accent);
			color: var(--mobile-nav-accent-on-fill);
		}
		.mobile-menu {
			position: absolute;
			top: calc(44px + var(--top-inset));
			left: 0.5rem;
			right: 0.5rem;
			display: flex;
			flex-direction: column;
			gap: 0.2rem;
			padding: 0.55rem;
			border: 1px solid var(--border-strong);
			border-top: none;
			border-radius: 0 0 10px 10px;
			background: var(--nav-bg);
			box-shadow: 0 8px 22px var(--shadow);
			box-sizing: border-box;
			max-height: calc(100vh - 44px - var(--top-inset) - 0.5rem);
			max-height: calc(100dvh - 44px - var(--top-inset) - 0.5rem);
			overflow-y: auto;
			overscroll-behavior: contain;
		}
		.mobile-menu-account {
			padding: 0.35rem 0.55rem 0.5rem;
			color: var(--nav-text-muted);
			font-size: 0.8rem;
			overflow-wrap: anywhere;
		}
		.mobile-menu a,
		.mobile-menu > button,
		.mobile-menu-preferences button {
			display: flex;
			align-items: center;
			min-height: 44px;
			box-sizing: border-box;
			padding: 0.55rem 0.7rem;
			border-radius: 7px;
			font-size: 0.95rem;
			text-align: left;
		}
		.mobile-menu a.active {
			background: rgba(255, 255, 255, 0.1);
			color: var(--nav-text-active);
		}
		.mobile-menu-preferences {
			display: grid;
			grid-template-columns: 1fr 1fr;
			gap: 0.45rem;
			padding-top: 0.35rem;
			border-top: 1px solid var(--border-strong);
		}
		.mobile-menu-preferences button {
			width: 100%;
			justify-content: center;
		}
		.mobile-menu .mobile-menu-logout {
			justify-content: center;
			margin-top: 0.25rem;
			border-color: var(--danger-text);
			color: #fff;
		}
	}
</style>
