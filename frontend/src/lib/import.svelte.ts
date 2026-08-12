import { activities, routes, type ActivityDetail, type Preset, type SavedRoute } from '$lib/api';

export interface ImportResult<T> {
	/** Filenames repeat - two apps both export "route.gpx" - so rows need
	 * an identity of their own to stay matched to the right file. */
	id: number;
	filename: string;
	status: 'waiting' | 'importing' | 'done' | 'failed';
	item?: T;
	error?: string;
}

export const ACCEPTED_FILES = '.gpx,.tcx,.fit';

function cueCount(route: SavedRoute): number {
	return route.legs.reduce((total, leg) => total + leg.maneuvers.length, 0);
}

/**
 * Sequential upload with a row per file.
 *
 * Shared by the Import button on the library, the window-wide drop target and
 * the activities page, so a file behaves the same however it arrives and
 * whatever it is being imported as.
 *
 * Files are uploaded one at a time rather than in a single request: each is
 * parsed server-side, and a whole Strava export in one request would sit
 * behind a single long timeout with no feedback.
 *
 * Generic over what an upload produces rather than duplicated per kind - the
 * sequencing, the batch counter and the by-id patching are the whole point of
 * this class, and none of them care what came back.
 */
class ImportQueue<T> {
	results = $state<ImportResult<T>[]>([]);
	busy = $state(false);
	/** Bumped when a batch finishes, so views can refresh themselves. */
	completedBatches = $state(0);
	#nextId = 1;
	#running = 0;
	#tail: Promise<void> = Promise.resolve();
	#upload: (file: File, preset: Preset) => Promise<T>;

	constructor(upload: (file: File, preset: Preset) => Promise<T>) {
		this.#upload = upload;
	}

	get imported(): T[] {
		return this.results.filter((r) => r.item).map((r) => r.item as T);
	}

	clear() {
		this.results = [];
	}

	async add(files: Iterable<File>, preset: Preset = 'road'): Promise<void> {
		const incoming = [...files];
		if (incoming.length === 0) return;

		const queued: ImportResult<T>[] = incoming.map((file) => ({
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
					const item = await this.#upload(file, preset);
					this.#patch(id, { status: 'done', item });
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
	#patch(id: number, patch: Partial<ImportResult<T>>) {
		this.results = this.results.map((result) =>
			result.id === id ? { ...result, ...patch } : result
		);
	}
}

export const importQueue = new ImportQueue<SavedRoute>((file, preset) =>
	routes.importFile(file, preset)
);

/**
 * Archives dropped somewhere that cannot import them itself.
 *
 * The window-wide drop handler lives in the layout and has no archive
 * machinery - that is page-local to /activities, which is the only place
 * that can show a job's progress. Rather than give the layout a second
 * implementation, it leaves the file here and the page drains it. The
 * alternative, sending a .zip to the single-ride endpoint, is what used to
 * happen: a 400, for the one file type that page advertises accepting.
 */
class PendingArchives {
	files = $state<File[]>([]);

	add(files: File[]) {
		this.files = [...this.files, ...files];
	}

	/** Takes everything waiting, leaving the queue empty. */
	drain(): File[] {
		const taken = this.files;
		this.files = [];
		return taken;
	}
}

export const pendingArchives = new PendingArchives();

/** Rides, not plans. No preset: an activity is never routed, so there is
 * nothing for a costing bundle to influence. */
export const activityImportQueue = new ImportQueue<ActivityDetail>((file) =>
	activities.importFile(file)
);

/** Routes whose track could not be matched, so they carry no turn cues. */
export function unmatched(results: ImportResult<SavedRoute>[]): ImportResult<SavedRoute>[] {
	return results.filter((r) => r.item && cueCount(r.item) === 0);
}

export { cueCount };
