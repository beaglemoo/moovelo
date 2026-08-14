const HEIGHT_KEY = 'moovelo:panel-height';
const COLLAPSED_KEY = 'moovelo:panel-collapsed';

/** Below this the handle plus a stats line no longer fit, so a drag never
 * commits a smaller open height - Collapse is the way to go smaller. */
export const MIN_PANEL_HEIGHT = 90;

/**
 * The planner's bottom info panel size. A per-browser layout preference (like
 * basemap, units and theme), so it lives in localStorage and is deliberately
 * NOT registered with onSessionReset - a rider's chosen panel height should
 * survive a logout.
 *
 * `height === null` means content-driven: the original `max-height: 55vh`
 * auto-sizing, untouched until the rider first drags the handle. Once set, the
 * panel is pinned to that pixel height (clamped to the viewport at use) so the
 * map keeps the rest. `collapsed` hides everything but the handle and the
 * one-line stats, giving the map almost the whole screen for placing waypoints.
 */
class PanelSize {
	height = $state<number | null>(null);
	collapsed = $state(false);

	/** Called once on mount (client only), matching the other moovelo:* reads. */
	load(): void {
		try {
			const raw = localStorage.getItem(HEIGHT_KEY);
			if (raw !== null) {
				const n = Number(raw);
				if (Number.isFinite(n) && n >= MIN_PANEL_HEIGHT) this.height = n;
			}
			this.collapsed = localStorage.getItem(COLLAPSED_KEY) === '1';
		} catch {
			/* corrupted or unavailable storage - stay content-driven */
		}
	}

	/** In-memory only - called on every pointermove of a drag, so it must NOT
	 * touch localStorage (a synchronous write per frame janks the drag). A drag
	 * also un-collapses. Persist once on release with persist(). */
	previewHeight(px: number): void {
		this.height = Math.max(MIN_PANEL_HEIGHT, Math.round(px));
		this.collapsed = false;
	}

	/** Commit the current height to storage. Called once when a drag ends, or
	 * directly (keyboard resize) where there is no per-frame stream. */
	setHeight(px: number): void {
		this.previewHeight(px);
		this.persist();
	}

	private persist(): void {
		try {
			localStorage.setItem(HEIGHT_KEY, String(this.height));
			localStorage.removeItem(COLLAPSED_KEY);
		} catch {
			/* storage unavailable - the size still applies for this session */
		}
	}

	toggleCollapsed(): void {
		this.collapsed = !this.collapsed;
		try {
			localStorage.setItem(COLLAPSED_KEY, this.collapsed ? '1' : '0');
		} catch {
			/* storage unavailable - still applies for this session */
		}
	}
}

export const panelSize = new PanelSize();
