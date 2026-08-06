<script lang="ts">
	import { goto } from '$app/navigation';
	import { auth, type AuthStatus } from '$lib/api';

	let status: AuthStatus | null = $state(null);
	let mode: 'login' | 'register' = $state('login');
	let email = $state('');
	let password = $state('');
	let error: string | null = $state(null);
	let busy = $state(false);

	auth.status().then((s) => {
		status = s;
		if (s.setup_required) mode = 'register';
	});

	const passwordLogin = $derived.by(() => status?.password_login ?? true);

	const title = $derived.by(() => {
		if (!passwordLogin) return 'Log in';
		if (status?.setup_required) return 'Create the admin account';
		return mode === 'register' ? 'Create an account' : 'Log in';
	});

	async function submit(event: SubmitEvent) {
		event.preventDefault();
		busy = true;
		error = null;
		try {
			if (mode === 'register') await auth.register(email, password);
			else await auth.login(email, password);
			await goto('/');
		} catch (err) {
			error = err instanceof Error ? err.message : 'Something went wrong';
		} finally {
			busy = false;
		}
	}
</script>

<div class="wrap">
	<form class="card" onsubmit={submit}>
		<h1>Komoot-lite</h1>
		<h2>{title}</h2>
		{#if passwordLogin}
			{#if status?.setup_required}
				<p class="hint">First run: this account becomes the administrator.</p>
			{/if}
			<label>
				Email
				<input type="email" bind:value={email} required autocomplete="email" />
			</label>
			<label>
				Password
				<input
					type="password"
					bind:value={password}
					required
					minlength="8"
					autocomplete={mode === 'register' ? 'new-password' : 'current-password'}
				/>
			</label>
			{#if error}
				<p class="error">{error}</p>
			{/if}
			<button type="submit" disabled={busy}>
				{mode === 'register' ? 'Create account' : 'Log in'}
			</button>
		{/if}
		{#if status?.oidc.enabled}
			{#if passwordLogin}
				<div class="divider">or</div>
			{/if}
			<a class="sso" href="/api/auth/oidc/login">Continue with {status.oidc.name}</a>
		{/if}
		{#if status && !status.setup_required && status.signups_enabled}
			<button
				type="button"
				class="link"
				onclick={() => (mode = mode === 'login' ? 'register' : 'login')}
			>
				{mode === 'login' ? 'Need an account? Register' : 'Have an account? Log in'}
			</button>
		{/if}
	</form>
</div>

<style>
	.wrap {
		height: 100%;
		display: flex;
		align-items: center;
		justify-content: center;
		background: #fdf6e3;
	}
	.card {
		background: #fff;
		border: 1px solid #eee8d5;
		border-radius: 12px;
		box-shadow: 0 4px 16px #0001;
		padding: 2rem;
		width: min(340px, 90vw);
		display: flex;
		flex-direction: column;
		gap: 0.9rem;
	}
	h1 {
		margin: 0;
		font-size: 1.3rem;
		color: #d33682;
	}
	h2 {
		margin: 0;
		font-size: 1.05rem;
		font-weight: 600;
		color: #073642;
	}
	.hint {
		margin: 0;
		font-size: 0.85rem;
		color: #586e75;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
		font-size: 0.9rem;
		color: #586e75;
	}
	input {
		border: 1px solid #ccc;
		border-radius: 6px;
		padding: 0.5rem 0.6rem;
		font: inherit;
	}
	button[type='submit'] {
		background: #d33682;
		color: #fff;
		border: none;
		border-radius: 8px;
		padding: 0.55rem;
		font: inherit;
		font-weight: 600;
		cursor: pointer;
	}
	button[type='submit']:disabled {
		opacity: 0.6;
	}
	.link {
		background: none;
		border: none;
		color: #268bd2;
		font: inherit;
		font-size: 0.85rem;
		cursor: pointer;
	}
	.error {
		margin: 0;
		color: #dc322f;
		font-size: 0.85rem;
	}
	.divider {
		text-align: center;
		color: #93a1a1;
		font-size: 0.8rem;
	}
	.sso {
		display: block;
		text-align: center;
		border: 1px solid #268bd2;
		color: #268bd2;
		border-radius: 8px;
		padding: 0.5rem;
		font-weight: 600;
		text-decoration: none;
	}
	.sso:hover {
		background: #268bd212;
	}
</style>
