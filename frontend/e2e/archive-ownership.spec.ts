import { expect, test } from '@playwright/test';
import { fileURLToPath } from 'node:url';

// `archive` and `pollTimer` in activities/+page.svelte used to be single,
// unkeyed variables shared by every startArchive() call. The Import button
// only disables once `archive` is set from the initial POST's response -
// not from the click itself - so a user whose first zip's upload is slow can
// still submit a second zip while the first has no tracked job yet. When the
// first's delayed response finally lands, it used to overwrite whatever the
// second (later-started, already-finished) job had put on screen, with
// nothing to say the display had gone stale.

const password = 'e2e-archive-ownership-password-1';
const zipA = fileURLToPath(new URL('./fixtures/archive-a.zip', import.meta.url));
const zipB = fileURLToPath(new URL('./fixtures/archive-b.zip', import.meta.url));

function statusPayload(id: string, filename: string, status: 'running' | 'done') {
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

test('a second archive import is not clobbered by the first, slower one', async ({ page }) => {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(
		!(status.setup_required || (status.signups_enabled && status.password_login)),
		'needs a fresh DB or SIGNUPS_ENABLED=true with password login'
	);
	const email = `e2e-archive-ownership-${Date.now()}@example.com`;
	const registered = await page.request.post('/api/auth/register', { data: { email, password } });
	expect(registered.ok()).toBeTruthy();

	// Controls exactly when job A's initial POST resolves, so the test can
	// submit job B while A is still in flight - the real, reachable window:
	// the Import button only disables once `archive` is set, which does not
	// happen until the initial POST resolves.
	let releaseA: () => void = () => {};
	const aGate = new Promise<void>((resolve) => (releaseA = resolve));
	let postCount = 0;

	await page.route('**/api/activities/import/archive', async (route) => {
		postCount += 1;
		if (postCount === 1) {
			await aGate;
			await route.fulfill({ json: statusPayload('job-a', 'archive-a.zip', 'running') });
		} else {
			await route.fulfill({ json: statusPayload('job-b', 'archive-b.zip', 'running') });
		}
	});
	await page.route('**/api/activities/import/archive/job-a', async (route) => {
		await route.fulfill({ json: statusPayload('job-a', 'archive-a.zip', 'running') });
	});
	await page.route('**/api/activities/import/archive/job-b', async (route) => {
		await route.fulfill({ json: statusPayload('job-b', 'archive-b.zip', 'done') });
	});

	await page.goto('/activities');
	const importButton = page.getByRole('button', { name: /Import rides/ });

	const chooser1 = page.waitForEvent('filechooser');
	await importButton.click();
	(await chooser1).setFiles(zipA);

	// The button must still be enabled - job A's initial POST has not
	// resolved yet, so the app has no idea a job is running. This is a
	// positive control: if this ever fails, the race below is no longer
	// reachable and the rest of the test proves nothing.
	await expect(importButton).toBeEnabled();

	const chooser2 = page.waitForEvent('filechooser');
	await importButton.click();
	(await chooser2).setFiles(zipB);

	// Let B's card appear and finish.
	await expect(page.locator('.archive')).toContainText('archive-b.zip', { timeout: 10_000 });
	await expect(page.locator('.archive.done')).toBeVisible({ timeout: 10_000 });

	// Now release A's long-delayed initial response.
	releaseA();
	await page.waitForTimeout(1000);

	// The archive card must still show B - the most recently *initiated*
	// import - not have reverted to A, which was superseded before it ever
	// got its first response.
	await expect(page.locator('.archive')).toContainText('archive-b.zip');
	await expect(page.locator('.archive.done')).toBeVisible();
});
