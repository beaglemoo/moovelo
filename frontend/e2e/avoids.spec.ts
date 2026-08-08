import { expect, test } from '@playwright/test';

// Avoids ("not that road", Valhalla exclude_locations): right-click the
// route line to mark a point to route around, which re-plans the route with
// it excluded. Session-only - never offered off the route line, and never
// saved with the route (the saved geometry already reflects it).

const email = `e2e-avoids-${Date.now()}@example.com`;
const password = 'e2e-avoids-password-1';

test('avoid a road from the context menu, then remove it', async ({ page }) => {
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
	// Let the map settle before clicking onto it.
	await page.waitForTimeout(2500);

	// The canvas shrinks as the results panel below it grows (elevation,
	// waypoint list), so click coordinates are recomputed from the live
	// bounding box before every interaction rather than captured once.
	async function canvasPoint(dx: number, dy: number): Promise<[number, number]> {
		const box = (await canvas.boundingBox())!;
		return [box.x + box.width / 2 + dx, box.y + box.height / 2 + dy];
	}

	// Two waypoints on a diagonal - the first click retries until it lands:
	// MapView only attaches its interaction handlers on the map's `load`
	// event, so an early click silently no-ops when tiles are still arriving.
	const markers = page.locator('.maplibregl-marker');
	await expect(async () => {
		await page.mouse.click(...(await canvasPoint(-40, -30)));
		await expect(markers).toHaveCount(1, { timeout: 1_000 });
	}).toPass({ timeout: 30_000 });
	await page.mouse.click(...(await canvasPoint(40, 30)));
	const saveButton = page.getByRole('button', { name: 'Save', exact: true });
	await expect(saveButton).toBeEnabled({ timeout: 30_000 });

	// Save it, so a later "route recomputes" check can look for "Save
	// changes" reappearing rather than "Save" simply staying enabled.
	await saveButton.click();
	await page.getByPlaceholder('Route name').fill('Avoids e2e');
	// Scoped to the dialog: the toolbar Save is still in the DOM behind it.
	await page.locator('form.dialog').getByRole('button', { name: 'Save', exact: true }).click();
	await expect(page.getByRole('button', { name: 'Saved', exact: true })).toBeVisible({
		timeout: 10_000
	});

	// An empty-canvas right-click, off the route line, must not offer
	// "Avoid this road" - only "Isochrone from here" and friends live there.
	await page.mouse.click(...(await canvasPoint(150, -150)), { button: 'right' });
	await expect(page.getByRole('menuitem', { name: 'Route from here' })).toBeVisible();
	await expect(page.getByRole('menuitem', { name: 'Avoid this road' })).toHaveCount(0);
	await page.keyboard.press('Escape');

	// The route line runs roughly along the diagonal between the two
	// waypoints, but Valhalla snaps it to real roads - so retry a few points
	// along that diagonal until one actually lands on the line.
	const avoidItem = page.getByRole('menuitem', { name: 'Avoid this road' });
	const onLineOffsets: [number, number][] = [
		[0, 0],
		[-8, -6],
		[8, 6],
		[-16, -12],
		[16, 12]
	];
	let attempt = 0;
	await expect(async () => {
		const [dx, dy] = onLineOffsets[attempt % onLineOffsets.length];
		attempt += 1;
		await page.mouse.click(...(await canvasPoint(dx, dy)), { button: 'right' });
		await expect(avoidItem).toBeVisible({ timeout: 1_000 });
	}).toPass({ timeout: 30_000 });
	await avoidItem.click();

	// The chip row appears and the route recomputes (re-planned with the
	// point excluded), turning "Saved" back into "Save changes".
	const avoidedRoads = page.getByRole('group', { name: 'Avoided roads' });
	await expect(avoidedRoads).toBeVisible();
	await expect(avoidedRoads.getByText('Avoid 1')).toBeVisible();
	await expect(page.getByRole('button', { name: 'Save changes', exact: true })).toBeVisible({
		timeout: 30_000
	});

	// Removing it via the chip's own button clears the row.
	await page.getByRole('button', { name: 'Remove avoid 1' }).click();
	await expect(avoidedRoads).toHaveCount(0);

	// Undo brings it back, same as any other planner edit.
	await page.getByRole('button', { name: 'Undo', exact: true }).click();
	await expect(page.getByRole('group', { name: 'Avoided roads' })).toBeVisible();
});
