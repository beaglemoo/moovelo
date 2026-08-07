/**
 * Whether the planner is holding edits that have not been saved.
 *
 * Lives outside the planner because the window-wide file drop is handled in
 * the layout, and importing navigates away - which would otherwise discard
 * those edits without asking.
 */
class UnsavedWork {
	dirty = $state(false);
}

export const unsaved = new UnsavedWork();
