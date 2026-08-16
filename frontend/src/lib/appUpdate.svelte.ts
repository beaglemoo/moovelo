// Knowing that a newer build exists, and letting the rider take it.
//
// The service worker deliberately never activates itself (no skipWaiting - see
// src/service-worker.ts for why: forcing it mid-session purges the running
// tab's cache and then 404s its next lazily-loaded route chunk). The cost of
// that safety is silence: an installed iOS home-screen app is resumed from the
// background rather than reloaded, so it can keep running a weeks-old shell and
// never say so. This store closes that gap without giving up the safety - the
// new worker still waits, and only a deliberate tap activates it, immediately
// followed by a reload, so nothing is purged underneath a live page.
class AppUpdate {
	/** A newer build has installed and is waiting to take over. */
	ready = $state(false);
	/** The rider has asked for it; the reload is on its way. */
	applying = $state(false);
	#registration: ServiceWorkerRegistration | null = null;
	#watching = false;

	async watch(): Promise<void> {
		// Runs from onMount, so this is the browser - but a page served over
		// plain HTTP, or a browser with workers disabled, has no serviceWorker
		// at all, and the PWA e2e project blocks them outright.
		if (this.#watching || typeof navigator === 'undefined' || !('serviceWorker' in navigator)) {
			return;
		}
		this.#watching = true;
		const registration = await navigator.serviceWorker.getRegistration();
		if (!registration) return;
		this.#registration = registration;

		// Already waiting when the app started: the update landed during a
		// previous run of this tab.
		if (registration.waiting) this.ready = true;

		registration.addEventListener('updatefound', () => {
			const installing = registration.installing;
			if (!installing) return;
			installing.addEventListener('statechange', () => {
				// `installed` with no controlling worker is the very first
				// install, which is not an update and must not be announced.
				if (installing.state === 'installed' && navigator.serviceWorker.controller) {
					this.ready = true;
				}
			});
		});

		// The check that actually matters on a phone. A home-screen app is
		// resumed, not reloaded, so without asking on resume the browser may
		// never look for a new worker and the app sits stale indefinitely -
		// which is the reported symptom this exists for.
		document.addEventListener('visibilitychange', () => {
			if (!document.hidden) void this.check();
		});
		window.addEventListener('focus', () => void this.check());
	}

	async check(): Promise<void> {
		try {
			await this.#registration?.update();
		} catch {
			// Offline, or the server is down. Nothing to report: the app keeps
			// running the version it has, which is the whole point of the cache.
		}
	}

	/** Activate the waiting build and reload onto it. */
	apply(): void {
		const waiting = this.#registration?.waiting;
		if (!waiting || this.applying) return;
		this.applying = true;
		// Reload when the new worker takes control, not on a timer: the page
		// must not reload before the worker that will serve it is in charge, or
		// it reloads onto the old cache and the update appears not to work.
		navigator.serviceWorker.addEventListener('controllerchange', this.#onControllerChange, {
			once: true
		});
		waiting.postMessage({ type: 'SKIP_WAITING' });
	}

	// A bound property rather than a method so `{ once: true }` removal and any
	// future removeEventListener refer to the same function object.
	#onControllerChange = () => {
		window.location.reload();
	};
}

export const appUpdate = new AppUpdate();
