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
		applyStatusBarColour();
	}
}

/**
 * iOS paints an installed app's status strip with `theme-color`. Giving it the
 * BAR's colour made the strip and the bar one continuous block, which read as a
 * single oversized slab once the bar gained its scrim inset. The page
 * background instead keeps them visibly separate in either theme.
 *
 * A media-scoped <meta> cannot do this: it follows prefers-color-scheme, and a
 * rider who picks light while the OS is dark would get a dark strip above a
 * light app. So it is set from the same place that applies the theme, and
 * mirrored by the pre-hydration guard in app.html for the very first paint.
 */
export const STATUS_BAR_COLOUR = { light: '#fdf6e3', dark: '#002b36' } as const;

function applyStatusBarColour(): void {
	const meta = document.querySelector('meta[name="theme-color"]');
	if (!meta) return;
	const dark =
		document.documentElement.dataset.theme === 'dark' ||
		(document.documentElement.dataset.theme === undefined &&
			window.matchMedia('(prefers-color-scheme: dark)').matches);
	meta.setAttribute('content', dark ? STATUS_BAR_COLOUR.dark : STATUS_BAR_COLOUR.light);
}

export const theme = new Theme();
