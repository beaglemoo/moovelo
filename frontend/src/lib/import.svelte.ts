import {
	activities,
	ApiError,
	routes,
	type ActivityDetail,
	type ArchiveImportStatus,
	type Preset,
	type SavedRoute
} from '$lib/api';
import { Latest, Poller } from '$lib/latest';

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

/** One archive attempt described for its whole life by a single phase value,
 * rather than a job object and an error string that each cover part of it.
 * The upload itself is a phase, so there is no gap between states to fall
 * into - see the long history in the ArchiveImport comment below. */
export type Attempt =
	| { phase: 'uploading'; filename: string }
	| { phase: 'tracking'; job: ArchiveImportStatus }
	| { phase: 'failed'; filename: string; message: string };

/**
 * A Strava-archive import, tracked at module scope so it survives navigation.
 *
 * The single-ride ImportQueue and pendingArchives already live here for the
 * same reason. The archive attempt used to be component-local $state on
 * /activities, so leaving the page and coming back mid-import destroyed the
 * card and re-enabled the Import button while the backend job ran on - a
 * seventh instance of this corner's recurring shape. The phase design is kept
 * verbatim; only its lifetime moves from the component to here, and the poll
 * is no longer stopped on unmount (that teardown was the bug).
 */
class ArchiveImport {
	attempt = $state<Attempt | null>(null);
	/** Bumped when an attempt reaches a terminal phase, so the page can
	 * refresh its list - the same signal ImportQueue.completedBatches gives. */
	completed = $state(0);
	// Replaced per run rather than reused: Poller.stop() is permanent, and
	// starting a second import must end the first one's polling for good.
	#poll = new Poller();
	// Whoever started last owns the card; an earlier upload's response landing
	// afterwards is discarded rather than reverting to a superseded attempt.
	#owner = new Latest();

	/** True while an upload is in flight or its job is queued/running. Read off
	 * the same value the card renders, so button and card cannot disagree. */
	get busy(): boolean {
		if (this.attempt?.phase === 'uploading') return true;
		if (this.attempt?.phase !== 'tracking') return false;
		return this.attempt.job.status === 'queued' || this.attempt.job.status === 'running';
	}

	dismiss() {
		this.attempt = null;
	}

	async start(file: File): Promise<void> {
		const token = this.#owner.claim();
		this.#poll.stop();
		this.#poll = new Poller();
		// Straight from whatever was there to this attempt's own first phase, so
		// there is no instant where the page shows nothing while an upload runs.
		this.attempt = { phase: 'uploading', filename: file.name };
		try {
			const started = await activities.importArchive(file);
			if (!this.#owner.isCurrent(token)) return;
			this.attempt = { phase: 'tracking', job: started };
		} catch (err) {
			if (!this.#owner.isCurrent(token)) return;
			this.attempt = {
				phase: 'failed',
				filename: file.name,
				message: err instanceof Error ? err.message : 'Could not read that archive'
			};
			return;
		}
		await this.#pollUntilDone(token);
	}

	async #pollUntilDone(token: number): Promise<void> {
		const poller = this.#poll;
		await poller.run(async () => {
			if (!this.#owner.isCurrent(token)) return false;
			if (this.attempt?.phase !== 'tracking') return false;
			const job = this.attempt.job;
			if (job.status !== 'queued' && job.status !== 'running') return false;
			try {
				const next = await activities.archiveStatus(job.id);
				if (!this.#owner.isCurrent(token)) return false;
				this.attempt = { phase: 'tracking', job: next };
			} catch (err) {
				if (!this.#owner.isCurrent(token)) return false;
				// Only a 404 means the job is genuinely gone - a restart, or
				// eviction once enough later jobs finished. A 500 or a dropped
				// connection used to look identical: the card vanished mid-import
				// with nothing said, which reads as "it lost my rides".
				if (err instanceof ApiError && err.status === 404) {
					this.attempt = null;
				} else {
					this.attempt = {
						phase: 'failed',
						filename: job.filename,
						message: err instanceof Error ? err.message : 'Lost track of that import'
					};
				}
				return false;
			}
			return true;
		}, 1500);
		if (this.#owner.isCurrent(token)) this.completed += 1;
	}
}

export const archiveImport = new ArchiveImport();

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
