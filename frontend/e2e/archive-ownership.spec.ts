import { expect, test } from '@playwright/test';
import { registerOrSkip, windowDropReady } from './support/auth';

// Two archive imports started before the first has reported anything: whoever
// started LAST must own the card, and the earlier one's late response must be
// discarded rather than reverting the display to a job that was superseded
// before it ever said a word.
//
// The route into that race has moved. It used to be the Import button, which
// stayed live during an upload because the busy state was read from a job
// object that did not exist yet - so a user with a slow first upload could
// simply click again. That window is now closed at the source ('uploading' is
// an explicit phase, set synchronously), and the first test here pins it shut.
//
// The race is still reachable, because the button is not the only way in: a
// dropped .zip goes through the window-level drop handler in +layout.svelte
// straight to pendingArchives, never consulting the button's disabled state.
// So the ownership guard is still load-bearing, and the second test drives it
// the way that can still happen.

const password = 'e2e-archive-ownership-password-1';

function job(id: string, filename: string, status: 'running' | 'done') {
	return {
		id,
		filename,
		status,
		total: 10,
		imported: status === 'done' ? 10 : 3,
		failed: 0,
		skipped: 0,
		duplicates: 0,
		error: null,
		problems: []
	};
}

/** Drops a .zip on the window, the way +layout.svelte actually receives one.
 * The bytes are an empty zip's End Of Central Directory record; the upload is
 * mocked, so nothing ever parses them. */
async function dropZip(page: import('@playwright/test').Page, name: string) {
	await page.evaluate((filename) => {
		const eocd = new Uint8Array(22);
		eocd.set([0x50, 0x4b, 0x05, 0x06]);
		const transfer = new DataTransfer();
		transfer.items.add(new File([eocd], filename, { type: 'application/zip' }));
		window.dispatchEvent(
			new DragEvent('drop', { dataTransfer: transfer, bubbles: true, cancelable: true })
		);
	}, name);
}

test('the Import button cannot start a second archive while one is uploading', async ({ page }) => {
	await registerOrSkip(page, 'e2e-archive-own-button', password);

	let release: () => void = () => {};
	const gate = new Promise<void>((resolve) => (release = resolve));
	await page.route('**/api/activities/import/archive', async (route) => {
		await gate;
		await route.fulfill({ json: job('job-a', 'archive-a.zip', 'done') });
	});
	await page.route('**/api/activities/import/archive/job-a', async (route) => {
		await route.fulfill({ json: job('job-a', 'archive-a.zip', 'done') });
	});

	await page.goto('/activities');
	const importButton = page.getByRole('button', { name: /Import rides|Importing/ });
	// Not just hydration: the drop handler ignores files until the layout has
	// resolved the session, and the button being visible proves neither -
	// this spec's own 1-in-20 flake was a drop landing in that window.
	await windowDropReady(page);
	await expect(importButton).toBeVisible();

	await dropZip(page, 'archive-a.zip');

	// The upload is in flight and the button says so. This is the assertion
	// that used to be its exact opposite: the spec relied on the button
	// staying enabled here to reach the race at all.
	await expect(page.locator('.archive')).toContainText('archive-a.zip', { timeout: 10_000 });
	await expect(importButton).toBeDisabled();

	release();
	await expect(page.locator('.archive.done')).toBeVisible({ timeout: 10_000 });
});

test('a second dropped archive is not clobbered by the first, slower one', async ({ page }) => {
	await registerOrSkip(page, 'e2e-archive-own-drop', password);

	// A's POST is held open so B can be started while A has no tracked job -
	// the real window, reachable by drop because a drop never looks at the
	// button.
	let releaseA: () => void = () => {};
	const aGate = new Promise<void>((resolve) => (releaseA = resolve));
	let postCount = 0;

	await page.route('**/api/activities/import/archive', async (route) => {
		postCount += 1;
		if (postCount === 1) {
			await aGate;
			await route.fulfill({ json: job('job-a', 'archive-a.zip', 'running') });
		} else {
			await route.fulfill({ json: job('job-b', 'archive-b.zip', 'running') });
		}
	});
	await page.route('**/api/activities/import/archive/job-a', async (route) => {
		await route.fulfill({ json: job('job-a', 'archive-a.zip', 'running') });
	});
	await page.route('**/api/activities/import/archive/job-b', async (route) => {
		await route.fulfill({ json: job('job-b', 'archive-b.zip', 'done') });
	});

	await page.goto('/activities');
	await windowDropReady(page);

	await dropZip(page, 'archive-a.zip');
	await expect(page.locator('.archive')).toContainText('archive-a.zip', { timeout: 10_000 });

	// B starts while A's POST is still open.
	await dropZip(page, 'archive-b.zip');
	await expect(page.locator('.archive')).toContainText('archive-b.zip', { timeout: 10_000 });
	await expect(page.locator('.archive.done')).toBeVisible({ timeout: 10_000 });

	// Now release A's long-delayed response. It must be discarded. Wait for
	// the page to actually receive it, then let two frames render - if the
	// ownership guard ever regresses, the stale job is applied by the
	// response's own then-handler and painted by the next frame, so the card
	// would read archive-a by now. A fixed 1s sleep proved the same thing,
	// slower and only probabilistically.
	const staleResponse = page.waitForResponse(
		(response) =>
			response.url().endsWith('/api/activities/import/archive') &&
			response.request().method() === 'POST'
	);
	releaseA();
	await staleResponse;
	await page.evaluate(
		() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))
	);

	await expect(page.locator('.archive')).toContainText('archive-b.zip');
	await expect(page.locator('.archive.done')).toBeVisible();
});
