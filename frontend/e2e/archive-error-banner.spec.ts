import { expect, test } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { canRegister } from './support/auth';

// The archive card and the page-level error banner used to be related by
// comparing their MESSAGE TEXT: the banner hid itself whenever the card
// happened to be showing the same string. Two different uploads failing with
// the same message is not unusual - "Failed to fetch" is what every dropped
// connection produces - so a brand-new upload's failure could be swallowed by
// an older, undismissed card that was still on screen attributing it to a
// different file entirely.
//
// The fix is not a better comparison. startArchive() now clears the card, so
// the card can only ever describe the current attempt, and the two can no
// longer disagree about which upload they mean.

const password = 'e2e-archive-error-banner-password-1';
const zipA = fileURLToPath(new URL('./fixtures/archive-a.zip', import.meta.url));
const zipB = fileURLToPath(new URL('./fixtures/archive-b.zip', import.meta.url));

// The one message both failures carry. Any shared text reproduces this; a
// generic one is simply the likeliest in the wild.
const SHARED_ERROR = 'Could not read that archive';

test('a second upload failing with a familiar message is still reported', async ({ page }) => {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(!canRegister(status), 'needs a fresh DB or SIGNUPS_ENABLED=true with password login');
	const email = `e2e-archive-error-banner-${Date.now()}@example.com`;
	const registered = await page.request.post('/api/auth/register', { data: { email, password } });
	expect(registered.ok()).toBeTruthy();

	let postCount = 0;
	await page.route('**/api/activities/import/archive', async (route) => {
		postCount += 1;
		if (postCount === 1) {
			// Accepted, so a job exists and the card starts polling.
			await route.fulfill({
				json: {
					id: 'job-a',
					filename: 'archive-a.zip',
					status: 'running',
					total: 10,
					imported: 3,
					failed: 0,
					skipped: 0,
					duplicates: 0,
					error: null,
					problems: []
				}
			});
		} else {
			// Rejected outright - no job is ever created, so only the banner
			// can carry this failure.
			await route.fulfill({ status: 400, json: { detail: SHARED_ERROR } });
		}
	});

	// Job A ends in exactly the same message the second POST will fail with.
	await page.route('**/api/activities/import/archive/job-a', async (route) => {
		await route.fulfill({
			json: {
				id: 'job-a',
				filename: 'archive-a.zip',
				status: 'error',
				total: 10,
				imported: 3,
				failed: 7,
				skipped: 0,
				duplicates: 0,
				error: SHARED_ERROR,
				problems: []
			}
		});
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

	// The failure must be visible. Before the fix the banner was suppressed
	// because the stale card's text matched, and the only thing on screen was
	// that card - still naming archive-a.zip.
	await expect(page.locator('p.error')).toContainText(SHARED_ERROR, { timeout: 10_000 });

	// And nothing may still be attributing this to the first file.
	await expect(page.locator('.archive')).toHaveCount(0);
});
