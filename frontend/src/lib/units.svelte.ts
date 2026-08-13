import type { UnitSystem } from '$lib/format';

const STORAGE_KEY = 'moovelo:units';

/**
 * The rider's metric/imperial display preference. A per-browser choice, not
 * session data, so it lives in localStorage (like basemap and overlay toggles)
 * and is deliberately NOT registered with onSessionReset - it should survive a
 * logout, unlike the planner history or import queues.
 *
 * Read `units.system` inside a template or $derived and it re-renders live when
 * the toggle flips.
 */
class Units {
	system = $state<UnitSystem>('metric');

	/** Called once on mount (client only), matching the other moovelo:* reads. */
	load(): void {
		try {
			const raw = localStorage.getItem(STORAGE_KEY);
			if (raw === 'imperial' || raw === 'metric') this.system = raw;
		} catch {
			/* corrupted or unavailable storage - stay on the default */
		}
	}

	set(system: UnitSystem): void {
		this.system = system;
		try {
			localStorage.setItem(STORAGE_KEY, system);
		} catch {
			/* storage unavailable - the choice still applies for this session */
		}
	}

	toggle(): void {
		this.set(this.system === 'metric' ? 'imperial' : 'metric');
	}
}

export const units = new Units();
