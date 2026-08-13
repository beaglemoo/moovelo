import type { BicycleCostingOptions, Preset, RouteResponse, RouteSource, Waypoint } from '$lib/api';
import { onSessionReset } from '$lib/session';

/** One point in planner history: everything a mutator changes as an input,
 * captured before the mutation lands. */
export interface PlannerSnapshot {
	waypoints: Waypoint[];
	preset: Preset;
	costingOptions: BicycleCostingOptions | null;
	source: RouteSource;
	/** The saved-route identity at capture time, restored by applySnapshot
	 * exactly as captured - null included. Undoing past the point a route was
	 * saved therefore detaches it, which is correct: what is on screen is no
	 * longer what that library row holds, so offering "Save" (a new row) is
	 * honest, where keeping the row attached would let the next save
	 * overwrite it with unrelated content. */
	savedId: string | null;
	savedName: string | null;
	/** Points to route around - see reroute()'s exclude_locations. Session-
	 * only, same as the rest of this snapshot: not persisted with a saved
	 * route. */
	avoidLocations: Waypoint[];
	/** Null for every ordinary edit. Set only by actions that replace the
	 * route OUTPUT without changing any input (none today; adopting an
	 * alternate route will be the first) - replaying inputs through
	 * reroute() cannot reproduce those, so applySnapshot restores this
	 * exact response instead of refetching. */
	routeOverride: RouteResponse | null;
}

// Bounded memory: routeOverride entries carry full snapshots, and 50 of them
// is an acceptable ceiling for one planner session.
const HISTORY_MAX = 50;

function cloneSnapshot(snap: PlannerSnapshot): PlannerSnapshot {
	return {
		waypoints: snap.waypoints.map((wp) => ({ ...wp })),
		preset: snap.preset,
		costingOptions: snap.costingOptions ? { ...snap.costingOptions } : null,
		source: snap.source,
		avoidLocations: snap.avoidLocations.map((wp) => ({ ...wp })),
		savedId: snap.savedId,
		savedName: snap.savedName,
		routeOverride: snap.routeOverride
	};
}

/** Multi-step undo/redo over the planner's inputs. A class instance
 * singleton, the same pattern as unsaved.svelte.ts, so it can be reached
 * from +page.svelte without threading it through props. */
class PlannerHistory {
	past: PlannerSnapshot[] = $state([]);
	future: PlannerSnapshot[] = $state([]);

	canUndo = $derived(this.past.length > 0);
	canRedo = $derived(this.future.length > 0);

	// A fresh edit invalidates whatever redo pointed at, and the caller
	// (a mutator) always deep-copies via cloneSnapshot so a later
	// waypoints.splice on the live array never reaches back into history.
	push(snapshot: PlannerSnapshot) {
		this.future = [];
		this.past = [...this.past, cloneSnapshot(snapshot)];
		if (this.past.length > HISTORY_MAX) this.past = this.past.slice(this.past.length - HISTORY_MAX);
	}

	undo(current: PlannerSnapshot): PlannerSnapshot | null {
		if (this.past.length === 0) return null;
		this.future = [...this.future, cloneSnapshot(current)];
		const snap = this.past[this.past.length - 1];
		this.past = this.past.slice(0, -1);
		return snap;
	}

	redo(current: PlannerSnapshot): PlannerSnapshot | null {
		if (this.future.length === 0) return null;
		this.past = [...this.past, cloneSnapshot(current)];
		const snap = this.future[this.future.length - 1];
		this.future = this.future.slice(0, -1);
		return snap;
	}

	clear() {
		this.past = [];
		this.future = [];
	}
}

export const history = new PlannerHistory();

// Logging out must not leave the next account's fresh planner able to undo
// into the previous rider's route - the singleton outlives a client-side
// logout the same way it outlives a page navigation.
onSessionReset(() => history.clear());
