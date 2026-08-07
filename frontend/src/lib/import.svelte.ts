import { routes, type Preset, type SavedRoute } from '$lib/api';

export interface ImportResult {
	/** Filenames repeat - two apps both export "route.gpx" - so rows need
	 * an identity of their own to stay matched to the right file. */
	id: number;
	filename: string;
	status: 'waiting' | 'importing' | 'done' | 'failed';
	route?: SavedRoute;
	error?: string;
}

export const ACCEPTED_FILES = '.gpx,.tcx,.fit';

function cueCount(route: SavedRoute): number {
	return route.legs.reduce((total, leg) => total + leg.maneuvers.length, 0);
}

/**
 * Shared by the Import button on the library and the window-wide drop target,
 * so a file behaves the same however it arrives.
 *
 * Files are uploaded one at a time rather than in a single request: each is
 * map-matched server-side, and a whole Strava export in one request would sit
 * behind a single long timeout with no feedback.
 */
class ImportQueue {
	results = $state<ImportResult[]>([]);
	busy = $state(false);
	/** Bumped when a batch finishes, so views can refresh themselves. */
	completedBatches = $state(0);
	#nextId = 1;
	#running = 0;
	#tail: Promise<void> = Promise.resolve();

	get imported(): SavedRoute[] {
		return this.results.filter((r) => r.route).map((r) => r.route as SavedRoute);
	}

	/** Routes whose track could not be matched, so they carry no turn cues. */
	get unmatched(): ImportResult[] {
		return this.results.filter((r) => r.route && cueCount(r.route) === 0);
	}

	clear() {
		this.results = [];
	}

	async add(files: Iterable<File>, preset: Preset = 'road'): Promise<void> {
		const incoming = [...files];
		if (incoming.length === 0) return;

		const queued: ImportResult[] = incoming.map((file) => ({
			id: this.#nextId++,
			filename: file.name,
			status: 'waiting'
		}));
		this.results = [...this.results, ...queued];

		this.#running += 1;
		this.busy = true;

		// Batches run one after another. Dropping files while a picked import
		// is still going would otherwise interleave, and the first batch to
		// finish would clear `busy` while the other was still uploading.
		const run = this.#tail.then(async () => {
			for (const [offset, file] of incoming.entries()) {
				const { id } = queued[offset];
				this.#patch(id, { status: 'importing' });
				try {
					const route = await routes.importFile(file, preset);
					this.#patch(id, { status: 'done', route });
				} catch (err) {
					this.#patch(id, {
						status: 'failed',
						error: err instanceof Error ? err.message : 'Import failed'
					});
				}
			}
		});
		this.#tail = run.catch(() => {});

		try {
			await run;
		} finally {
			this.#running -= 1;
			if (this.#running === 0) {
				this.busy = false;
				this.completedBatches += 1;
			}
		}
	}

	/** Patched by id: rows can be dismissed or added while a batch runs, so a
	 * positional index is not stable. */
	#patch(id: number, patch: Partial<ImportResult>) {
		this.results = this.results.map((result) =>
			result.id === id ? { ...result, ...patch } : result
		);
	}
}

export const importQueue = new ImportQueue();
export { cueCount };
