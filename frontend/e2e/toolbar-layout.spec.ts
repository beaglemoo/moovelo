import { expect, test } from '@playwright/test';
import { canRegister, NEEDS_REGISTRATION } from './support/auth';

// The toolbar wraps onto a second (or third) row on anything narrower than
// a wide desktop once a route is saved and grows Save/GPX/FIT/the route
// name/Wahoo. The search bar below it used to sit at a fixed 52px, which
// assumed one row and put it on top of the wrapped row instead of below it.

const password = 'e2e-toolbar-layout-password-1';

test('the search bar never overlaps a wrapped toolbar', async ({ page }) => {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(!canRegister(status), NEEDS_REGISTRATION);
	const email = `e2e-toolbar-layout-${Date.now()}@example.com`;
	const registered = await page.request.post('/api/auth/register', { data: { email, password } });
	expect(registered.ok()).toBeTruthy();

	await page.goto('/');
	const canvas = page.locator('.map canvas').first();
	await expect(canvas).toBeVisible();
	await page.waitForTimeout(2500);

	// Plan and save a route: Save, GPX, FIT, the route name and Wahoo all
	// join the toolbar once there is a saved route, which is what pushes it
	// onto a second row at ordinary laptop widths.
	const box = (await canvas.boundingBox())!;
	const cx = box.x + box.width / 2;
	const cy = box.y + box.height / 2;
	await page.mouse.click(cx - 40, cy - 30);
	await page.mouse.click(cx + 40, cy + 30);
	const save = page.getByRole('button', { name: 'Save', exact: true });
	await expect(save).toBeEnabled({ timeout: 30_000 });
	await save.click();
	await page.getByPlaceholder('Route name').fill('Toolbar layout route');
	await page.locator('.dialog').getByRole('button', { name: 'Save' }).click();
	await expect(page.getByText('Toolbar layout route')).toBeVisible();

	for (const width of [320, 480, 640, 820, 1024]) {
		await page.setViewportSize({ width, height: 800 });
		await page.waitForTimeout(150);
		const toolbarBox = await page.locator('.toolbar').boundingBox();
		const searchBox = await page.locator('.search-bar').boundingBox();
		if (!toolbarBox || !searchBox) continue;
		const overlapX =
			Math.min(toolbarBox.x + toolbarBox.width, searchBox.x + searchBox.width) -
			Math.max(toolbarBox.x, searchBox.x);
		const overlapY =
			Math.min(toolbarBox.y + toolbarBox.height, searchBox.y + searchBox.height) -
			Math.max(toolbarBox.y, searchBox.y);
		expect(overlapX > 0 && overlapY > 0, `overlap at width=${width}`).toBe(false);
	}
});
