import type { BicycleCostingOptions, Preset, RouteResponse, RouteSource, Waypoint } from '$lib/api';

/** One point in planner history: everything a mutator changes as an input,
 * captured before the mutation lands. */
export interface PlannerSnapshot {
	waypoints: Waypoint[];
	preset: Preset;
	costingOptions: BicycleCostingOptions | null;
	source: RouteSource;
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
