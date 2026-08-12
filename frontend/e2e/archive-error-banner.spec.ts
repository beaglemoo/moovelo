import { expect, test } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { registerOrSkip } from './support/auth';

// Two bugs, one cause, pinned together because the fix for the first produced
// the second.
//
// The archive card and a page-level error banner were two places the same
// failure could appear, so something had to decide when to show each. First
// that rule compared message TEXT, which hid a genuinely different upload's
// failure whenever the strings matched ("Failed to fetch" always matches).
// Clearing the card at the start of each attempt fixed that and opened a
// window with no card, no error and an enabled Import button while an upload
// was actually in flight.
//
// There is now one `attempt` value with an explicit phase, and no banner at
// all - so neither state is reachable. These two tests are what that claim
// means in practice.

const password = 'e2e-archive-error-banner-password-1';
const zipA = fileURLToPath(new URL('./fixtures/archive-a.zip', import.meta.url));
const zipB = fileURLToPath(new URL('./fixtures/archive-b.zip', import.meta.url));

// The one message both failures carry. Any shared text reproduces the
// original bug; a generic one is simply the likeliest in the wild.
const SHARED_ERROR = 'Could not read that archive';

function job(id: string, filename: string, status: string, error: string | null = null) {
	return {
		id,
		filename,
		status,
		total: 10,
		imported: status === 'done' ? 10 : 3,
		failed: status === 'error' ? 7 : 0,
		skipped: 0,
		duplicates: 0,
		error,
		problems: []
	};
}

test('a second upload failing with a familiar message is reported against its own file', async ({
	page
}) => {
	await registerOrSkip(page, 'e2e-archive-banner', password);

	let postCount = 0;
	await page.route('**/api/activities/import/archive', async (route) => {
		postCount += 1;
		if (postCount === 1) {
			await route.fulfill({ json: job('job-a', 'archive-a.zip', 'running') });
		} else {
			// Rejected outright - no job is ever created, so only the attempt
			// itself can carry this failure.
			await route.fulfill({ status: 400, json: { detail: SHARED_ERROR } });
		}
	});
	await page.route('**/api/activities/import/archive/job-a', async (route) => {
		await route.fulfill({ json: job('job-a', 'archive-a.zip', 'error', SHARED_ERROR) });
	});

	await page.goto('/activities');
	const importButton = page.getByRole('button', { name: /Import rides/ });

	const chooser1 = page.waitForEvent('filechooser');
	await importButton.click();
	(await chooser1).setFiles(zipA);

	// A's card settles into its error state, and is deliberately NOT dismissed.
	await expect(page.locator('.archive')).toContainText('archive-a.zip', { timeout: 10_000 });
	await expect(page.locator('.archive .error')).toContainText(SHARED_ERROR, { timeout: 10_000 });

	// Now a different file, which fails before any job exists to carry it.
	const chooser2 = page.waitForEvent('filechooser');
	await importButton.click();
	(await chooser2).setFiles(zipB);

	// The failure must be visible AND attributed to the file that actually
	// failed. The old text-comparison rule left archive-a.zip on screen owning
	// archive-b.zip's error.
	await expect(page.locator('.archive')).toContainText('archive-b.zip', { timeout: 10_000 });
	await expect(page.locator('.archive .error')).toContainText(SHARED_ERROR);
	await expect(page.locator('.archive')).not.toContainText('archive-a.zip');

	// Exactly one place says it. A page-level banner saying the same thing is
	// what needed a rule to suppress it, and rules are what got this wrong
	// twice.
	await expect(page.locator('p.error')).toHaveCount(0);
});

test('an upload in flight always says so', async ({ page }) => {
	await registerOrSkip(page, 'e2e-archive-inflight', password);

	// Held open so the test can observe the window between picking a file and
	// the POST resolving - the window that previously showed nothing at all.
	let release: () => void = () => {};
	const gate = new Promise<void>((resolve) => (release = resolve));

	await page.route('**/api/activities/import/archive', async (route) => {
		await gate;
		await route.fulfill({ json: job('job-c', 'archive-a.zip', 'done') });
	});
	await page.route('**/api/activities/import/archive/job-c', async (route) => {
		await route.fulfill({ json: job('job-c', 'archive-a.zip', 'done') });
	});

	await page.goto('/activities');
	const importButton = page.getByRole('button', { name: /Import rides|Importing/ });

	const chooser = page.waitForEvent('filechooser');
	await importButton.click();
	(await chooser).setFiles(zipA);

	// While the upload is genuinely in flight: the card exists, names the
	// file, and the button is disabled. Previously all three were false - no
	// card, and a live button reading "Import rides".
	await expect(page.locator('.archive')).toContainText('archive-a.zip', { timeout: 10_000 });
	await expect(importButton).toBeDisabled();

	release();
	await expect(page.locator('.archive.done')).toBeVisible({ timeout: 10_000 });
	await expect(importButton).toBeEnabled();
});
