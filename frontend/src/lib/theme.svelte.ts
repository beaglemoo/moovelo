const STORAGE_KEY = 'moovelo:theme';

export type ThemeMode = 'system' | 'light' | 'dark';

/**
 * The rider's light/dark display preference. A per-browser choice, not
 * session data, so it lives in localStorage (like units) and is deliberately
 * NOT registered with onSessionReset - it should survive a logout.
 *
 * `mode` is the rider's stated preference; 'system' defers to
 * prefers-color-scheme. Applying it means setting/removing
 * `document.documentElement.dataset.theme`, which the CSS in +layout.svelte
 * reads to pick token values - see the FOUC-guard inline script in app.html,
 * which does the same thing synchronously before hydration.
 */
class Theme {
	mode = $state<ThemeMode>('system');

	/** Called once on mount (client only), matching the other moovelo:* reads. */
	load(): void {
		try {
			const raw = localStorage.getItem(STORAGE_KEY);
			if (raw === 'light' || raw === 'dark' || raw === 'system') this.mode = raw;
		} catch {
			/* corrupted or unavailable storage - stay on the default */
		}
		this.apply();
	}

	set(mode: ThemeMode): void {
		this.mode = mode;
		try {
			localStorage.setItem(STORAGE_KEY, mode);
		} catch {
			/* storage unavailable - the choice still applies for this session */
		}
		this.apply();
	}

	cycle(): void {
		const next: ThemeMode =
			this.mode === 'system' ? 'light' : this.mode === 'light' ? 'dark' : 'system';
		this.set(next);
	}

	private apply(): void {
		if (typeof document === 'undefined') return;
		if (this.mode === 'system') {
			delete document.documentElement.dataset.theme;
		} else {
			document.documentElement.dataset.theme = this.mode;
		}
	}
}

export const theme = new Theme();
