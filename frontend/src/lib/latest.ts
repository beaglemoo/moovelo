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
	#running = false;

	/** Runs `step` after `delayMs`, and again after each `step` resolves to
	 * `true`, until `step` resolves `false` or stop() is called. Resolves
	 * once polling has ended either way.
	 *
	 * A second run() on a live instance ends the first rather than racing
	 * it. That is not politeness - one instance holds one #timer handle, so
	 * two concurrent runs would leave the earlier handle overwritten and
	 * unreachable, and stop() could then only cancel the later one. The
	 * orphan would fire a step after teardown, which is precisely the
	 * failure this class exists to prevent. A review found exactly that
	 * reachable at one of the call sites, so the constraint is enforced here
	 * rather than left as an unstated contract for the next caller to
	 * discover.
	 */
	async run(step: () => Promise<boolean>, delayMs: number): Promise<void> {
		if (this.#running) this.#endCurrent();
		this.#running = true;
		const generation = Symbol('run');
		this.#generation = generation;

		while (!this.#stopped && this.#generation === generation) {
			const again = await new Promise<boolean>((resolve) => {
				this.#timer = setTimeout(() => {
					this.#timer = null;
					// A step that throws synchronously - a non-async callback -
					// would otherwise never settle this promise and hang run()
					// forever, beyond the reach of stop().
					try {
						step().then(resolve, () => resolve(false));
					} catch {
						resolve(false);
					}
				}, delayMs);
			});
			if (this.#stopped || !again || this.#generation !== generation) break;
		}
		if (this.#generation === generation) this.#running = false;
	}

	#generation: symbol | null = null;

	/** Ends whatever run() is currently in flight without stopping the
	 * poller for good, so a fresh run can take over cleanly. */
	#endCurrent(): void {
		if (this.#timer) clearTimeout(this.#timer);
		this.#timer = null;
		this.#generation = null;
	}

	/** Stops this poller for good. Safe to call from onDestroy even while a
	 * step's own request is in flight - the in-flight step still runs, but
	 * its result is never scheduled again. */
	stop(): void {
		this.#stopped = true;
		this.#running = false;
		this.#endCurrent();
	}
}
