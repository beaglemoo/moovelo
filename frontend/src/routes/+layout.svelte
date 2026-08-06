<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { ApiError, auth, type UserInfo } from '$lib/api';
	import type { Snippet } from 'svelte';

	let { children }: { children: Snippet } = $props();

	let user: UserInfo | null = $state(null);

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
					void goto('/login');
				}
			});
	});

	async function logout() {
		await auth.logout();
		user = null;
		await goto('/login');
	}
</script>

<div class="shell">
	{#if user}
		<nav>
			<span class="brand">Moovelo</span>
			<a href="/" class:active={page.url.pathname === '/'}>Planner</a>
			<a href="/library" class:active={page.url.pathname === '/library'}>Library</a>
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
	@media (max-width: 560px) {
		.email {
			display: none;
		}
		nav {
			gap: 0.7rem;
		}
	}
</style>
