import { expect, test } from '@playwright/test';

// Loop generator (services/loop.py): right-click a point, ask for a loop of
// a chosen distance, and pick one of the returned candidates. The picked
// candidate must land as an ordinary, already-routed, saveable route - no
// second /api/route call - exactly like every other edit in the planner.

const email = `e2e-loop-${Date.now()}@example.com`;
const password = 'e2e-loop-password-1';

test('loop from here produces a usable route', async ({ page }) => {
	// A real search: up to 8 bearings x up to 6 binary-search steps, each a
	// Valhalla round trip plus a surface lookup. Slow but bounded server-side
	// (LOOP_TIMEOUT_S) - this just needs to outlast that.
	test.setTimeout(180_000);

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
	// Let the map settle before right-clicking onto it.
	await page.waitForTimeout(2500);

	// Same map centre other specs (smoke, gradient) route real waypoints
	// through - a well-covered inland spot in the default England view.
	// The right-click retries until the menu appears: MapView only attaches
	// its interaction handlers on the map's `load` event, so an early click
	// silently no-ops when tiles are still arriving.
	const loopItem = page.getByRole('menuitem', { name: 'Loop from here' });
	await expect(async () => {
		const box = (await canvas.boundingBox())!;
		await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2, {
			button: 'right'
		});
		await expect(loopItem).toBeVisible({ timeout: 1_000 });
	}).toPass({ timeout: 30_000 });
	await loopItem.click();

	const targetInput = page.getByLabel('Target distance (km)');
	await expect(targetInput).toBeVisible();
	await targetInput.fill('20');
	await page.getByRole('button', { name: 'Find loops' }).click();

	// The best-scoring candidate card, once the search settles.
	const useButton = page.getByRole('button', { name: 'Use this loop' }).first();
	await expect(useButton).toBeVisible({ timeout: 150_000 });
	await useButton.click();

	// A picked loop is an ordinary route: Save lights up, and the stats row
	// shows a real distance, with no further routing call in between.
	await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeEnabled({
		timeout: 10_000
	});
	await expect(page.locator('.stats span').first()).toContainText('km');
});
