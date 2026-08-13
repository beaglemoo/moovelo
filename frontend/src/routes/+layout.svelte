<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { ApiError, auth, type UserInfo } from '$lib/api';
	import { activityImportQueue, importQueue, pendingArchives } from '$lib/import.svelte';
	import { unsaved } from '$lib/unsaved.svelte';
	import type { Snippet } from 'svelte';

	let { children }: { children: Snippet } = $props();

	let user: UserInfo | null = $state(null);
	let dropping = $state(false);

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
					// the planner's guard does not block this forced navigation.
					unsaved.leaving = true;
					void goto('/login');
				}
			});
	});

	async function logout() {
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
			return;
		}
		user = null;
		unsaved.leaving = true;
		await goto('/login');
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
/>

<div class="shell">
	{#if user}
		<nav>
			<span class="brand">Moovelo</span>
			<a href="/" class:active={page.url.pathname === '/'}>Planner</a>
			<a href="/library" class:active={page.url.pathname === '/library'}>Library</a>
			<a href="/activities" class:active={page.url.pathname === '/activities'}>Activities</a>
			<a href="/settings" class:active={page.url.pathname === '/settings'}>Settings</a>
			{#if user.is_admin}
				<a href="/admin" class:active={page.url.pathname === '/admin'}>Admin</a>
			{/if}
			<span class="spacer"></span>
			<span class="email">{user.email}</span>
			<button type="button" onclick={logout}>Log out</button>
		</nav>
	{/if}
	<main>
		{@render children()}
	</main>
	{#if dropping}
		<div class="dropzone">
			<p>Drop GPX, TCX or FIT files to import</p>
		</div>
	{/if}
</div>

<style>
	:global(html, body) {
		margin: 0;
		height: 100%;
	}
	.shell {
		display: flex;
		flex-direction: column;
		height: 100vh;
		height: 100dvh;
		font-family:
			system-ui,
			-apple-system,
			sans-serif;
	}
	nav {
		display: flex;
		align-items: center;
		gap: 1rem;
		padding: 0 0.9rem;
		height: 42px;
		background: #073642;
		color: #eee8d5;
		flex-shrink: 0;
	}
	.brand {
		font-weight: 700;
		margin-right: 0.5rem;
	}
	nav a {
		color: #93a1a1;
		text-decoration: none;
		font-size: 0.95rem;
	}
	nav a.active,
	nav a:hover {
		color: #fdf6e3;
	}
	.spacer {
		flex: 1;
	}
	.email {
		font-size: 0.85rem;
		color: #93a1a1;
	}
	nav button {
		border: 1px solid #586e75;
		background: transparent;
		color: #eee8d5;
		border-radius: 6px;
		padding: 0.25rem 0.7rem;
		font: inherit;
		font-size: 0.85rem;
		cursor: pointer;
	}
	main {
		flex: 1;
		min-height: 0;
	}
	.dropzone {
		position: fixed;
		inset: 0;
		z-index: 50;
		display: grid;
		place-items: center;
		background: rgba(7, 54, 66, 0.75);
		color: #fdf6e3;
		font-size: 1.2rem;
		pointer-events: none;
	}
	.dropzone p {
		border: 2px dashed #93a1a1;
		border-radius: 10px;
		padding: 2rem 3rem;
	}
	@media (max-width: 560px) {
		.email {
			display: none;
		}
		nav {
			gap: 0.7rem;
		}
	}
</style>
