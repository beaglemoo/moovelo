import { expect, test } from '@playwright/test';
import { canRegister, NEEDS_REGISTRATION } from './support/auth';

// The metric/imperial toggle: plan a route, flip units, and confirm the
// displayed figures change from km to mi and the choice survives a reload
// (it lives in localStorage). Runs against the dev compose stack like the
// other e2e specs.

const email = `e2e-units-${Date.now()}@example.com`;
const password = 'e2e-units-password-1';

test('the units toggle flips distances to imperial and persists', async ({ page }) => {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(!canRegister(status), NEEDS_REGISTRATION);

	const registered = await page.request.post('/api/auth/register', {
		data: { email, password }
	});
	expect(registered.ok()).toBeTruthy();

	await page.goto('/');
	const canvas = page.locator('.map canvas').first();
	await expect(canvas).toBeVisible();
	// Let the map settle before clicking waypoints onto it.
	await page.waitForTimeout(2500);

	const markers = page.locator('.maplibregl-marker');
	// MapView only attaches its click handler on the map's `load` event, so the
	// first click retries until a marker actually lands.
	await expect(async () => {
		const box = (await canvas.boundingBox())!;
		await page.mouse.click(box.x + box.width / 2 - 80, box.y + box.height / 2 - 60);
		await expect(markers).toHaveCount(1, { timeout: 1_000 });
	}).toPass({ timeout: 30_000 });

	const box = (await canvas.boundingBox())!;
	await page.mouse.click(box.x + box.width / 2 + 80, box.y + box.height / 2 + 60);
	await expect(markers).toHaveCount(2);

	const stats = page.locator('.stats');
	const distanceStat = stats.locator('span').first();
	await expect(distanceStat).toContainText('km', { timeout: 30_000 });
	// Ascent starts metric too.
	await expect(stats).toContainText(/\bm\b/);

	const toggle = page.locator('button.units-toggle');
	await expect(toggle).toHaveText('km');
	await toggle.click();

	await expect(toggle).toHaveText('mi');
	await expect(distanceStat).toContainText('mi');
	await expect(distanceStat).not.toContainText('km');
	// Ascent flipped to feet in the same bar.
	await expect(stats).toContainText('ft');

	// The choice is a per-browser localStorage preference: a reload keeps it,
	// even though the in-progress route itself is gone.
	await page.reload();
	await expect(page.locator('button.units-toggle')).toHaveText('mi');
});
