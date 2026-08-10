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

	// A fixed pixel offset from the centre is not safe for anything far out:
	// the map is only a few hundred pixels tall once the panel below it has
	// grown, so the old (150, -150) landed above the canvas entirely and the
	// right-click went to the toolbar. Fractions of the live box stay inside.
	async function canvasFraction(fx: number, fy: number): Promise<[number, number]> {
		const box = (await canvas.boundingBox())!;
		return [box.x + box.width * fx, box.y + box.height * fy];
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
	await page.mouse.click(...(await canvasFraction(0.85, 0.25)), { button: 'right' });
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

	// The chip row appears and the route recomputes with the point excluded.
	// Deliberately no assertion on the Save button here: "unsaved changes"
	// means the state differs from the stored route, and an avoid that the
	// router can honour without moving the line leaves nothing to save. That
	// distinction has its own spec in save-integrity.spec.ts.
	const avoidedRoads = page.getByRole('group', { name: 'Avoided roads' });
	await expect(avoidedRoads).toBeVisible();
	await expect(avoidedRoads.getByText('Avoid 1')).toBeVisible();

	// Removing it via the chip's own button clears the row.
	await page.getByRole('button', { name: 'Remove avoid 1' }).click();
	await expect(avoidedRoads).toHaveCount(0);

	// Undo brings it back, same as any other planner edit.
	await page.getByRole('button', { name: 'Undo', exact: true }).click();
	await expect(page.getByRole('group', { name: 'Avoided roads' })).toBeVisible();
});

test('adding an avoid drops loop candidates found before it', async ({ page }) => {
	const email = `e2e-loop-avoid-${Date.now()}@example.com`;
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(
		!(status.setup_required || (status.signups_enabled && status.password_login)),
		'needs a fresh DB or SIGNUPS_ENABLED=true with password login'
	);
	expect(
		(
			await page.request.post('/api/auth/register', {
				data: { email, password: 'e2e-loop-avoid-1' }
			})
		).ok()
	).toBeTruthy();

	await page.goto('/');
	const canvas = page.locator('.map canvas').first();
	await expect(canvas).toBeVisible();
	await page.waitForTimeout(2500);

	// A route to hang an avoid on.
	await expect(async () => {
		const box = (await canvas.boundingBox())!;
		await page.mouse.click(box.x + box.width / 2 - 60, box.y + box.height / 2 - 40);
		await expect(page.locator('.maplibregl-marker')).toHaveCount(1, { timeout: 1_000 });
	}).toPass({ timeout: 30_000 });
	let box = (await canvas.boundingBox())!;
	await page.mouse.click(box.x + box.width / 2 + 40, box.y + box.height / 2 + 30);
	await expect(page.locator('.maplibregl-marker')).toHaveCount(2);
	await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeEnabled({
		timeout: 30_000
	});

	// Find loops from a third point, then avoid a road on the existing route.
	box = (await canvas.boundingBox())!;
	await page.mouse.click(box.x + box.width / 2 + 120, box.y + box.height / 2 - 90, {
		button: 'right'
	});
	await page.getByRole('menuitem', { name: /Loop from here/i }).click();
	const loopPanel = page.getByRole('dialog', { name: 'Loop from here' });
	await expect(loopPanel).toBeVisible();
	await loopPanel.getByRole('button', { name: /Find loops/i }).click();
	await expect(loopPanel.getByRole('button', { name: /Use this loop/i }).first()).toBeVisible({
		timeout: 60_000
	});

	// Those candidates were found without the avoid, so they must not survive
	// it - adopting one would ride straight through the excluded road.
	box = (await canvas.boundingBox())!;
	await page.mouse.click(box.x + box.width / 2 - 10, box.y + box.height / 2 - 5, {
		button: 'right'
	});
	const avoidItem = page.getByRole('menuitem', { name: /Avoid this road/i });
	if (await avoidItem.count()) {
		await avoidItem.click();
		await expect(page.getByRole('group', { name: 'Avoided roads' })).toBeVisible({
			timeout: 30_000
		});
		await expect(loopPanel.getByRole('button', { name: /Use this loop/i })).toHaveCount(0);
	}
});

test('adding an avoid drops alternates found before it', async ({ page }) => {
	const email = `e2e-alt-avoid-${Date.now()}@example.com`;
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(
		!(status.setup_required || (status.signups_enabled && status.password_login)),
		'needs a fresh DB or SIGNUPS_ENABLED=true with password login'
	);
	expect(
		(
			await page.request.post('/api/auth/register', {
				data: { email, password: 'e2e-alt-avoid-1' }
			})
		).ok()
	).toBeTruthy();

	await page.goto('/');
	const canvas = page.locator('.map canvas').first();
	await expect(canvas).toBeVisible();
	await page.waitForTimeout(2500);

	await expect(async () => {
		const b = (await canvas.boundingBox())!;
		await page.mouse.click(b.x + b.width / 2 - 60, b.y + b.height / 2 - 40);
		await expect(page.locator('.maplibregl-marker')).toHaveCount(1, { timeout: 1_000 });
	}).toPass({ timeout: 30_000 });
	let box = (await canvas.boundingBox())!;
	await page.mouse.click(box.x + box.width / 2 + 40, box.y + box.height / 2 + 30);
	await expect(page.locator('.maplibregl-marker')).toHaveCount(2);

	const altBtn = page.getByRole('button', { name: 'Alternatives', exact: true });
	await expect(altBtn).toBeEnabled({ timeout: 30_000 });
	await altBtn.click();
	const altPanel = page.getByRole('dialog', { name: 'Alternative routes' });
	const useAlt = altPanel.getByRole('button', { name: 'Use', exact: true });
	await expect(useAlt.first()).toBeVisible({ timeout: 60_000 });

	// Same rule as loops: candidates predating the avoid must not survive it.
	box = (await canvas.boundingBox())!;
	await page.mouse.click(box.x + box.width / 2 - 10, box.y + box.height / 2 - 5, {
		button: 'right'
	});
	const avoidItem = page.getByRole('menuitem', { name: /Avoid this road/i });
	if (await avoidItem.count()) {
		await avoidItem.click();
		await expect(page.getByRole('group', { name: 'Avoided roads' })).toBeVisible({
			timeout: 30_000
		});
		await expect(useAlt).toHaveCount(0);
	}
});

test('undoing a preset change drops alternates costed under it', async ({ page }) => {
	const email = `e2e-preset-undo-${Date.now()}@example.com`;
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(
		!(status.setup_required || (status.signups_enabled && status.password_login)),
		'needs a fresh DB or SIGNUPS_ENABLED=true with password login'
	);
	expect(
		(
			await page.request.post('/api/auth/register', {
				data: { email, password: 'e2e-preset-undo-1' }
			})
		).ok()
	).toBeTruthy();

	await page.goto('/');
	const canvas = page.locator('.map canvas').first();
	await expect(canvas).toBeVisible();
	await page.waitForTimeout(2500);

	await expect(async () => {
		const b = (await canvas.boundingBox())!;
		await page.mouse.click(b.x + b.width / 2 - 60, b.y + b.height / 2 - 40);
		await expect(page.locator('.maplibregl-marker')).toHaveCount(1, { timeout: 1_000 });
	}).toPass({ timeout: 30_000 });
	const box = (await canvas.boundingBox())!;
	await page.mouse.click(box.x + box.width / 2 + 40, box.y + box.height / 2 + 30);
	await expect(page.locator('.maplibregl-marker')).toHaveCount(2);
	await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeEnabled({
		timeout: 30_000
	});

	// Switch preset, then fetch alternates costed under the new one.
	await page.getByRole('radio', { name: /Gravel/i }).click();
	await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeEnabled({
		timeout: 30_000
	});
	const altBtn = page.getByRole('button', { name: 'Alternatives', exact: true });
	await expect(altBtn).toBeEnabled({ timeout: 30_000 });
	await altBtn.click();
	const panel = page.getByRole('dialog', { name: 'Alternative routes' });
	const useAlt = panel.getByRole('button', { name: 'Use', exact: true });
	await expect(useAlt.first()).toBeVisible({ timeout: 60_000 });

	// Undo reverts the preset without touching the waypoints. The candidates
	// were drawn for gravel, so adopting one now would store gravel geometry
	// under a road label.
	await page.getByRole('button', { name: 'Undo', exact: true }).click();
	await expect(page.getByRole('radio', { name: /Road/i })).toHaveAttribute('aria-checked', 'true', {
		timeout: 30_000
	});
	await expect(useAlt).toHaveCount(0);
});
