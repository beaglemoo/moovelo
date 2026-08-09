import { expect, test } from '@playwright/test';

// Two ways a save used to persist something the rider never saw, both found
// by review pass 3 in the fixes review pass 2 had just made.

const password = 'e2e-save-integrity-1';

async function register(page: import('@playwright/test').Page, email: string) {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(
		!(status.setup_required || (status.signups_enabled && status.password_login)),
		'needs a fresh DB or SIGNUPS_ENABLED=true with password login'
	);
	const registered = await page.request.post('/api/auth/register', { data: { email, password } });
	expect(registered.ok()).toBeTruthy();
}

async function plant(page: import('@playwright/test').Page, count: number) {
	const canvas = page.locator('.map canvas').first();
	await expect(canvas).toBeVisible();
	await page.waitForTimeout(2500);
	const markers = page.locator('.maplibregl-marker');
	// MapView attaches its handlers on the map's `load` event, so the first
	// click retries until one actually lands.
	await expect(async () => {
		const box = (await canvas.boundingBox())!;
		await page.mouse.click(box.x + box.width / 2 - 60, box.y + box.height / 2 - 40);
		await expect(markers).toHaveCount(1, { timeout: 1_000 });
	}).toPass({ timeout: 30_000 });
	const box = (await canvas.boundingBox())!;
	for (let i = 1; i < count; i++) {
		await page.mouse.click(box.x + box.width / 2 + i * 55, box.y + box.height / 2 + i * 35);
	}
	await expect(markers).toHaveCount(count);
	return markers;
}

test('undoing past a save detaches the route from its library row', async ({ page }) => {
	await register(page, `e2e-detach-${Date.now()}@example.com`);
	await page.goto('/');
	await plant(page, 3);

	const saveBtn = page.getByRole('button', { name: 'Save', exact: true });
	await expect(saveBtn).toBeEnabled({ timeout: 30_000 });
	await saveBtn.click();
	await page.getByPlaceholder('Route name').fill('Detach test');
	await page.locator('form.dialog').getByRole('button', { name: 'Save' }).click();
	await expect(page.getByRole('button', { name: 'Saved', exact: true })).toBeVisible({
		timeout: 30_000
	});

	// Undo lands on a snapshot captured before the save, so what is on screen
	// is no longer that library row. The button must offer a NEW save: while
	// it still read "Save changes", saving here overwrote the stored route
	// with whatever the rider planned next.
	await page.getByRole('button', { name: 'Undo', exact: true }).click();
	await expect(page.locator('.maplibregl-marker')).toHaveCount(2);
	await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeVisible({
		timeout: 30_000
	});
	await expect(page.getByRole('button', { name: 'Save changes', exact: true })).toHaveCount(0);
});

test('a failed reroute blocks saving the stale geometry', async ({ page }) => {
	await register(page, `e2e-stale-${Date.now()}@example.com`);
	await page.goto('/');
	const markers = await plant(page, 2);

	const saveBtn = page.getByRole('button', { name: 'Save', exact: true });
	await expect(saveBtn).toBeEnabled({ timeout: 30_000 });

	// Now make routing fail, and edit. The previous line stays on screen
	// under an error banner - deliberately - but `route` no longer matches
	// `waypoints`, so saving that pair would store a track that does not
	// belong to its own waypoints.
	await page.route('**/api/route', (route) => route.abort());
	const box = (await page.locator('.map canvas').first().boundingBox())!;
	await page.mouse.click(box.x + box.width / 2 + 120, box.y + box.height / 2 - 90);
	await expect(markers).toHaveCount(3);
	await expect(saveBtn).toBeDisabled({ timeout: 30_000 });

	// Recovering re-enables it: the guard tracks whether the geometry matches,
	// not whether an error ever happened.
	await page.unroute('**/api/route');
	await page.mouse.click(box.x + box.width / 2 + 140, box.y + box.height / 2 - 60);
	await expect(markers).toHaveCount(4);
	await expect(saveBtn).toBeEnabled({ timeout: 30_000 });
});

test('an edit during a save in flight cannot persist a mismatched pair', async ({ page }) => {
	await register(page, `e2e-midsave-${Date.now()}@example.com`);
	await page.goto('/');
	const markers = await plant(page, 2);

	const saveBtn = page.getByRole('button', { name: 'Save', exact: true });
	await expect(saveBtn).toBeEnabled({ timeout: 30_000 });
	await saveBtn.click();
	await page.getByPlaceholder('Route name').fill('Mid-save edit');
	await page.locator('form.dialog').getByRole('button', { name: 'Save' }).click();
	await expect(page.getByRole('button', { name: 'Saved', exact: true })).toBeVisible({
		timeout: 30_000
	});

	// Capture what actually reaches the server.
	const writes: { waypoints: number; legs: number }[] = [];
	page.on('request', (req) => {
		if (!/\/api\/routes\/[0-9a-f-]{36}$/.test(req.url())) return;
		if (req.method() !== 'PATCH' && req.method() !== 'PUT') return;
		const body = req.postDataJSON();
		if (body?.waypoints && body?.snapshot)
			writes.push({ waypoints: body.waypoints.length, legs: body.snapshot.legs.length });
	});

	// Hold the surface fetch so the save parks inside it, then edit while it
	// is parked with routing held open, so the edit's reroute never lands.
	let releaseSurface = () => {};
	const surfaceHeld = new Promise<void>((resolve) => (releaseSurface = resolve));
	await page.route('**/api/route/surface', async (route) => {
		await surfaceHeld;
		await route.continue();
	});

	const box = (await page.locator('.map canvas').first().boundingBox())!;
	await page.mouse.click(box.x + box.width / 2 + 100, box.y + box.height / 2 + 70);
	await expect(markers).toHaveCount(3);
	await expect(page.getByRole('button', { name: 'Save changes', exact: true })).toBeEnabled({
		timeout: 30_000
	});
	await page.getByRole('button', { name: 'Save changes', exact: true }).click();

	await page.route('**/api/route', () => {});
	await page.mouse.click(box.x + box.width / 2 + 140, box.y + box.height / 2 + 110);
	await expect(markers).toHaveCount(4);
	releaseSurface();
	await page.waitForTimeout(2000);

	// Whatever was written must describe one ride: n waypoints, n-1 legs.
	for (const write of writes) expect(write.legs).toBe(write.waypoints - 1);
});

test('a reroute that lands after Clear does not resurrect the route', async ({ page }) => {
	await register(page, `e2e-late-${Date.now()}@example.com`);
	await page.goto('/');

	// Hold the routing response open so the second waypoint's reroute is still
	// in flight when Clear runs.
	await page.route('**/api/route', async (route) => {
		await new Promise((resolve) => setTimeout(resolve, 3000));
		await route.continue();
	});

	const markers = await plant(page, 2);
	await page.getByRole('button', { name: 'Clear', exact: true }).click();
	await expect(markers).toHaveCount(0);

	// The in-flight response lands here. It describes waypoints that no longer
	// exist, so it must be discarded rather than painting a route and its
	// stats panel back onto an empty map.
	await page.waitForTimeout(4500);
	await expect(markers).toHaveCount(0);
	await expect(page.locator('.panel')).toHaveCount(0);
});

test('redo does not restore a route captured while it was stale', async ({ page }) => {
	await register(page, `e2e-redo-stale-${Date.now()}@example.com`);
	await page.goto('/');
	const markers = await plant(page, 2);

	const saveBtn = page.getByRole('button', { name: 'Save', exact: true });
	const undoBtn = page.getByRole('button', { name: 'Undo', exact: true });
	const redoBtn = page.getByRole('button', { name: 'Redo', exact: true });
	await expect(saveBtn).toBeEnabled({ timeout: 30_000 });

	// Fail a reroute, so `route` belongs to the previous waypoints.
	await page.route('**/api/route', (route) => route.abort());
	const box = (await page.locator('.map canvas').first().boundingBox())!;
	await page.mouse.click(box.x + box.width / 2 + 120, box.y + box.height / 2 - 90);
	await expect(markers).toHaveCount(3);
	await expect(saveBtn).toBeDisabled({ timeout: 30_000 });

	// Recover and step back: undo captures the state we are leaving, which
	// must NOT carry that stale route forward as a restorable output.
	await page.unroute('**/api/route');
	await undoBtn.click();
	await expect(markers).toHaveCount(2);
	await expect(saveBtn).toBeEnabled({ timeout: 30_000 });

	// Redo with routing still failing: replaying the inputs must fail again
	// and keep Save shut. While redo restored the stale route verbatim, it
	// came back marked clean and Save was enabled on a mismatched pair.
	await page.route('**/api/route', (route) => route.abort());
	await redoBtn.click();
	await expect(markers).toHaveCount(3);
	await expect(saveBtn).toBeDisabled({ timeout: 30_000 });
});
