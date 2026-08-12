/**
 * Ownership for "latest wins" async state.
 *
 * The planner already has this shape for `route` (+page.svelte's
 * routeToken/claimRoute): every path that starts async work claims a token
 * first, and a response only gets applied if its token is still the current
 * one when it lands. A caller that starts newer work automatically
 * invalidates every earlier claim - nothing has to remember to check or
 * cancel the previous one, so a new call site cannot forget to guard itself.
 *
 * Without this, an earlier call that happens to resolve later silently wins
 * over a newer one - the archive-import card reverting to a stale, already-
 * superseded job's status, or a route/activity list flickering back to a
 * previous filter's results, are both this same shape.
 */
export class Latest {
	#token = 0;

	/** Claim ownership for a new piece of async work. Pass the returned token
	 * to isCurrent() before applying that work's result. */
	claim(): number {
		this.#token += 1;
		return this.#token;
	}

	/** Whether `token` still owns the latest claim - false means newer work
	 * has started since, and this result must be discarded rather than
	 * applied. */
	isCurrent(token: number): boolean {
		return token === this.#token;
	}
}

/**
 * A setTimeout poll loop that stops for good once told to, including a
 * request that was already in flight at that moment.
 *
 * `onDestroy(() => clearTimeout(pollTimer))` only cancels a *scheduled*
 * timer. If the previous timer had already fired and its request was in
 * flight at unmount, that request's own callback goes on to re-arm a new
 * timer that nothing is left to clear - the poll continues invisibly,
 * forever, after the component is gone. Checking a `stopped` flag right
 * after every await, before ever scheduling the next one, closes that gap
 * regardless of where in the cycle stop() is called.
 */
export class Poller {
	#timer: ReturnType<typeof setTimeout> | null = null;
	#stopped = false;

	/** Runs `step` after `delayMs`, and again after each `step` resolves to
	 * `true`, until `step` resolves `false` or stop() is called. Resolves
	 * once polling has ended either way. */
	async run(step: () => Promise<boolean>, delayMs: number): Promise<void> {
		while (!this.#stopped) {
			const again = await new Promise<boolean>((resolve) => {
				this.#timer = setTimeout(() => {
					this.#timer = null;
					step().then(resolve, () => resolve(false));
				}, delayMs);
			});
			if (this.#stopped || !again) return;
		}
	}

	/** Stops this poller for good. Safe to call from onDestroy even while a
	 * step's own request is in flight - the in-flight step still runs, but
	 * its result is never scheduled again. */
	stop(): void {
		this.#stopped = true;
		if (this.#timer) clearTimeout(this.#timer);
		this.#timer = null;
	}
}
