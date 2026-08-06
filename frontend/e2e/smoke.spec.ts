import { expect, test } from '@playwright/test';

// Smoke test: plan a route, save it, export GPX. Runs against the dev
// compose stack and needs password registration to be possible - either a
// fresh database (first user) or SIGNUPS_ENABLED=true. Skips otherwise.

const email = `e2e-${Date.now()}@example.com`;
const password = 'e2e-smoke-password-1';

test('plan, save, export GPX', async ({ page }) => {
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
	// Let the map settle before clicking waypoints onto it.
	await page.waitForTimeout(2500);

	const box = (await canvas.boundingBox())!;
	const cx = box.x + box.width / 2;
	const cy = box.y + box.height / 2;
	await page.mouse.click(cx - 40, cy - 30);
	await page.mouse.click(cx + 40, cy + 30);

	// Routing succeeded once Save lights up and the stats panel appears.
	const save = page.getByRole('button', { name: 'Save', exact: true });
	await expect(save).toBeEnabled({ timeout: 30_000 });

	await save.click();
	await page.getByPlaceholder('Route name').fill('E2E smoke route');
	await page.locator('.dialog').getByRole('button', { name: 'Save' }).click();
	await expect(page.getByText('E2E smoke route')).toBeVisible();

	await page.goto('/library');
	await expect(page.getByText('E2E smoke route')).toBeVisible();
	const downloadPromise = page.waitForEvent('download');
	await page.getByRole('link', { name: 'GPX' }).click();
	const download = await downloadPromise;
	expect(download.suggestedFilename()).toBe('e2e-smoke-route.gpx');
});
