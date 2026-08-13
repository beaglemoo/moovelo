/**
 * Whether the planner is holding edits that have not been saved.
 *
 * Lives outside the planner because the window-wide file drop is handled in
 * the layout, and importing navigates away - which would otherwise discard
 * those edits without asking.
 */
class UnsavedWork {
	dirty = $state(false);
	/** Set true when the app is intentionally tearing the session down (log
	 * out, or the forced bounce on an expired session) and navigating to
	 * /login. The planner's beforeNavigate guard skips a navigation while this
	 * is set: guarding a deliberate teardown just double-prompts and, if
	 * cancelled, strands a dead session. It is NOT keyed on the /login
	 * destination, because a browser Back to /login while still authenticated
	 * is an accidental exit that should still warn. Reset when the planner
	 * mounts, so the next editing session is guarded again. */
	leaving = $state(false);
}

export const unsaved = new UnsavedWork();
