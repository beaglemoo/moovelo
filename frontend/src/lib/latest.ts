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
	#generation: symbol | null = null;
	// The resolver for the iteration currently waiting. Holding it is the
	// whole point: clearing a timer cancels the thing that WOULD have
	// resolved, so without this every teardown path leaves run() awaiting a
	// promise nothing can ever settle.
	#settleCurrent: ((again: boolean) => void) | null = null;

	/** Runs `step` after `delayMs`, and again after each `step` resolves to
	 * `true`, until `step` resolves `false` or stop() is called. Resolves
	 * once polling has ended, by every route out.
	 *
	 * That last clause is load-bearing and was wrong twice. Three review
	 * passes broke this class in the same family, and the shape was always
	 * the same: a path that ENDS a run without SETTLING it. A cancelled
	 * timer resolves nothing, so a superseded or stopped run hung forever -
	 * and because both callers await run() to do their after-work, the
	 * after-work never happened. At one call site that meant a second
	 * dropped archive was silently never imported.
	 *
	 * So the invariant is stated once, here, and enforced in one place:
	 * #finish() is the only way a run ends, and it always settles. Adding a
	 * new exit path means calling it, not remembering a rule.
	 */
	async run(step: () => Promise<boolean>, delayMs: number): Promise<void> {
		// A second run on a live instance ends the first rather than racing
		// it: one instance holds one timer handle, so two concurrent runs
		// would leave the earlier unreachable and stop() could cancel only
		// the later one.
		if (this.#running) this.#finish(false);
		this.#running = true;
		const generation = Symbol('run');
		this.#generation = generation;

		while (!this.#stopped && this.#generation === generation) {
			const again = await new Promise<boolean>((resolve) => {
				this.#settleCurrent = resolve;
				this.#timer = setTimeout(() => {
					this.#timer = null;
					// A step that throws synchronously - a non-async callback -
					// would otherwise never settle this promise either.
					try {
						step().then(
							(more) => this.#settle(more),
							() => this.#settle(false)
						);
					} catch {
						this.#settle(false);
					}
				}, delayMs);
			});
			if (this.#stopped || !again || this.#generation !== generation) break;
		}
		if (this.#generation === generation) {
			this.#generation = null;
			this.#running = false;
		}
	}

	/** Settles the waiting iteration exactly once. */
	#settle(again: boolean): void {
		const resolve = this.#settleCurrent;
		this.#settleCurrent = null;
		resolve?.(again);
	}

	/** Ends the run in flight and settles it, so its caller stops awaiting.
	 * Every exit path goes through here. */
	#finish(again: boolean): void {
		if (this.#timer) clearTimeout(this.#timer);
		this.#timer = null;
		this.#generation = null;
		this.#settle(again);
	}

	/** Stops this poller for good. Safe to call from onDestroy even while a
	 * step's own request is in flight - the in-flight step still runs, but
	 * its result is never scheduled again, and run() resolves rather than
	 * leaving its caller waiting on a poll that will never continue. */
	stop(): void {
		this.#stopped = true;
		this.#running = false;
		this.#finish(false);
	}
}
