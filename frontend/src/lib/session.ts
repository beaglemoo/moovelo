/**
 * Session-scoped client state resets on logout.
 *
 * The planner's undo history, the import queues and the archive tracker all
 * live in module-scope singletons so they survive navigation between pages.
 * But a logout is a client-side navigation too - SvelteKit does not reload the
 * document - so those singletons also survive it, and the next account to log
 * in on the same tab inherits the previous user's undo stack, import results
 * and in-flight jobs. On a shared computer that is one user's route waypoints
 * and imported-file metadata shown to another.
 *
 * Each session-scoped store registers a reset callback here; logout() and the
 * expired-session bounce call resetSession(). A store added later is one
 * registration next to its singleton, not a new call site to remember at every
 * teardown - which is the shape that let two of these leak unnoticed. A store
 * that was never imported never registered and holds nothing to leak, so the
 * lazy registration is not a gap.
 */
const resetters = new Set<() => void>();

/** Register a callback run whenever the session tears down (logout, expiry). */
export function onSessionReset(reset: () => void): void {
	resetters.add(reset);
}

/** Clear every session-scoped store. Called on logout and the 401 bounce. */
export function resetSession(): void {
	for (const reset of resetters) reset();
}
