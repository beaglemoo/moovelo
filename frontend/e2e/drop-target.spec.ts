import { expect, test } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { canRegister } from './support/auth';

// The window-wide drop handler in +layout.svelte used to send every dropped
// file through importQueue (planned routes) and always navigate to
// /library - even a ride dropped straight onto /activities, and even though
// activities has its own queue for exactly this. That both mis-imported the
// file (as a route, map-matched through Valhalla, rather than an activity)
// and abandoned whatever /activities was doing (an in-flight archive poll)
// by forcing a navigation nobody asked for.

const password = 'e2e-drop-target-password-1';
const ride = fileURLToPath(new URL('./fixtures/tring-ride.gpx', import.meta.url));

/** Builds a DataTransfer in the page from a file on disk, the documented
 * Playwright pattern for testing native drag-and-drop without a real OS
 * drag. */
async function dataTransferFor(page: import('@playwright/test').Page, path: string, name: string) {
	const base64 = readFileSync(path).toString('base64');
	return page.evaluateHandle(
		async ({ base64, name }) => {
			const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
			const file = new File([bytes], name, { type: 'application/gpx+xml' });
			const dt = new DataTransfer();
			dt.items.add(file);
			return dt;
		},
		{ base64, name }
	);
}

test('dropping a ride on /activities imports it as an activity, not a planned route', async ({
	page
}) => {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(!canRegister(status), 'needs a fresh DB or SIGNUPS_ENABLED=true with password login');
	const email = `e2e-drop-target-${Date.now()}@example.com`;
	const registered = await page.request.post('/api/auth/register', { data: { email, password } });
	expect(registered.ok()).toBeTruthy();

	await page.goto('/activities');
	await expect(page.getByText('No rides yet')).toBeVisible();

	const dataTransfer = await dataTransferFor(page, ride, 'tring-ride.gpx');
	await page.dispatchEvent('body', 'dragover', { dataTransfer, bubbles: true, cancelable: true });
	await page.dispatchEvent('body', 'drop', { dataTransfer, bubbles: true, cancelable: true });

	// Wait for the upload to actually finish (a real round trip, not an
	// instant DOM change) before checking anything - the old bug's
	// `goto('/library')` and this assertion racing each other is exactly
	// what let an earlier, weaker version of this test pass by accident.
	const result = page.locator('.results li').first();
	await expect(result).toContainText('tring-ride.gpx', { timeout: 60_000 });
	await expect(result).toContainText('m ascent', { timeout: 60_000 });

	// Stayed on /activities - the drop must not navigate away and abandon
	// whatever this page was doing.
	await expect(page).toHaveURL(/\/activities$/);

	// Landed as an activity, not a route: a route's result row always
	// carries a cue count (or the "no turn cues" fallback) and a View link;
	// an activity's never does, because ImportResults only renders those
	// for an item with `legs`.
	await expect(result).not.toContainText('turn cues');
	await expect(result.getByRole('link', { name: 'View' })).toHaveCount(0);

	const row = page.locator('tbody tr', { hasText: 'E2E ride fixture' });
	await expect(row).toBeVisible({ timeout: 60_000 });

	// And it must not also have landed in the route library as a planned
	// route (the old, wrong destination).
	const libraryRoutes = await page.request.get('/api/routes').then((r) => r.json());
	expect(
		(libraryRoutes as Array<{ name: string }>).some((r) => r.name === 'E2E ride fixture')
	).toBe(false);
});

test('dropping a file on the planner still imports it as a planned route in /library', async ({
	page
}) => {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(!canRegister(status), 'needs a fresh DB or SIGNUPS_ENABLED=true with password login');
	const email = `e2e-drop-target-planner-${Date.now()}@example.com`;
	const registered = await page.request.post('/api/auth/register', { data: { email, password } });
	expect(registered.ok()).toBeTruthy();

	await page.goto('/');
	const canvas = page.locator('.map canvas').first();
	await expect(canvas).toBeVisible();

	const dataTransfer = await dataTransferFor(page, ride, 'tring-ride.gpx');
	await page.dispatchEvent('body', 'dragover', { dataTransfer, bubbles: true, cancelable: true });
	await page.dispatchEvent('body', 'drop', { dataTransfer, bubbles: true, cancelable: true });

	// The everywhere-else default: leaves the planner and lands in the
	// library, routed through Valhalla, which is what a file dropped
	// anywhere but /activities has always done.
	await expect(page).toHaveURL(/\/library$/, { timeout: 15_000 });
	const result = page.locator('.results li').first();
	await expect(result).toContainText('tring-ride.gpx', { timeout: 60_000 });
	await expect(result).toContainText('turn cues', { timeout: 60_000 });
});
