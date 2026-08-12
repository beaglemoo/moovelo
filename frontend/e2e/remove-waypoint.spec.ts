import { expect, test } from '@playwright/test';
import { canRegister } from './support/auth';

// Regression test: right-clicking a waypoint marker must open its context
// menu without inserting a phantom via point (markers bubble events to the
// canvas container, where the route-grab handler listens), and "Remove
// waypoint" must actually remove it.

const email = `e2e-remove-${Date.now()}@example.com`;
const password = 'e2e-remove-password-1';

test('remove waypoint via marker context menu', async ({ page }) => {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(!canRegister(status), 'needs a fresh DB or SIGNUPS_ENABLED=true with password login');

	const registered = await page.request.post('/api/auth/register', {
		data: { email, password }
	});
	expect(registered.ok()).toBeTruthy();

	const pageErrors: string[] = [];
	page.on('pageerror', (err) => pageErrors.push(err.message));

	await page.goto('/');
	const canvas = page.locator('.map canvas').first();
	await expect(canvas).toBeVisible();
	await page.waitForTimeout(2500);

	const box = (await canvas.boundingBox())!;
	const cx = box.x + box.width / 2;
	const cy = box.y + box.height / 2;
	await page.mouse.click(cx - 60, cy - 40);
	await page.mouse.click(cx, cy);
	await page.mouse.click(cx + 60, cy + 40);

	const markers = page.locator('.maplibregl-marker');
	await expect(markers).toHaveCount(3);
	await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeEnabled({
		timeout: 30_000
	});

	const mid = markers.nth(1);
	const mbox = (await mid.boundingBox())!;
	await page.mouse.click(mbox.x + mbox.width / 2, mbox.y + mbox.height / 2, {
		button: 'right'
	});

	const removeBtn = page.getByRole('menuitem', { name: 'Remove waypoint' });
	await expect(removeBtn).toBeVisible({ timeout: 3_000 });
	// The right-click itself must not have inserted a via point.
	await expect(markers).toHaveCount(3);

	await removeBtn.click();
	await expect(markers).toHaveCount(2, { timeout: 5_000 });
	expect(pageErrors).toEqual([]);
});
