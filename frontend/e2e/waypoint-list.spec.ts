import { expect, test } from '@playwright/test';

// Waypoint list panel (Phase 8, PR5): reorder via the up/down buttons and
// remove a waypoint via its row button. Native HTML5 drag-and-drop is the
// pointer path but is exercised manually on staging - headless Chromium
// does not reliably fire the drag event sequence Playwright would need to
// drive it, so CI drives the accessible/touch path (the buttons) instead,
// which is also what the component itself relies on for touch devices.

const email = `e2e-waypoints-${Date.now()}@example.com`;
const password = 'e2e-waypoints-password-1';

test('waypoint list: reorder and remove', async ({ page }) => {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(
		!(status.setup_required || (status.signups_enabled && status.password_login)),
		'needs a fresh DB or SIGNUPS_ENABLED=true with password login'
	);

	const registered = await page.request.post('/api/auth/register', {
		data: { email, password }
	});
	expect(registered.ok()).toBeTruthy();

	await page.goto('/');
	const canvas = page.locator('.map canvas').first();
	await expect(canvas).toBeVisible();

	const markers = page.locator('.maplibregl-marker');
	// The first click retries until it lands: MapView only attaches its
	// interaction handlers on the map's `load` event, so an early click
	// silently no-ops when tiles are still arriving.
	await expect(async () => {
		const box = (await canvas.boundingBox())!;
		await page.mouse.click(box.x + box.width / 2 - 100, box.y + box.height / 2 - 70);
		await expect(markers).toHaveCount(1, { timeout: 1_000 });
	}).toPass({ timeout: 30_000 });

	const box = (await canvas.boundingBox())!;
	const cx = box.x + box.width / 2;
	const cy = box.y + box.height / 2;
	await page.mouse.click(cx, cy);
	await page.mouse.click(cx + 120, cy + 90);
	await expect(markers).toHaveCount(3);

	const save = page.getByRole('button', { name: 'Save', exact: true });
	await expect(save).toBeEnabled({ timeout: 30_000 });

	// Three positional rows, always labelled by position rather than
	// identity - a 3-waypoint route has exactly one via.
	const rows = page.locator('.waypoints li');
	await expect(rows).toHaveCount(3);
	await expect(rows.nth(0)).toContainText('Start');
	await expect(rows.nth(1)).toContainText('Via 1');
	await expect(rows.nth(2)).toContainText('Finish');

	const distanceBefore = await page.locator('.stats span').first().textContent();

	// Move the finish waypoint up a slot. Labels stay positional (row 2 is
	// still "Finish" after the move), so the change is verified through the
	// route recomputing between two different points instead: distance
	// changes, and the marker count is unaffected by a reorder.
	await page.getByRole('button', { name: 'Move waypoint 3 up' }).click();
	await expect(async () => {
		const distanceAfter = await page.locator('.stats span').first().textContent();
		expect(distanceAfter).not.toBe(distanceBefore);
	}).toPass({ timeout: 10_000 });
	await expect(markers).toHaveCount(3);
	await expect(rows).toHaveCount(3);

	// Remove a row's waypoint via its remove button.
	await page.getByRole('button', { name: 'Remove waypoint 1' }).click();
	await expect(rows).toHaveCount(2);
	await expect(markers).toHaveCount(2);
});
