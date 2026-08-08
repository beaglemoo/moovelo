import { expect, test } from '@playwright/test';

// Isochrone overlay from the map's context menu: "how far can I get in N
// minutes" from a chosen point. Origin-anchored rather than route-anchored,
// so this plans a short route first and checks Clear route takes the
// isochrone with it too, not just the route.

const email = `e2e-isochrone-${Date.now()}@example.com`;
const password = 'e2e-isochrone-password-1';

test('show and hide an isochrone from the context menu', async ({ page }) => {
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
	// POIs), so click coordinates are recomputed from the live bounding box
	// before every interaction rather than captured once - a stale centre
	// can land on the panel instead of the map.
	async function canvasPoint(dx: number, dy: number): Promise<[number, number]> {
		const box = (await canvas.boundingBox())!;
		return [box.x + box.width / 2 + dx, box.y + box.height / 2 + dy];
	}

	// A short route, so "Clear route" is available on the context menu and
	// there is something for the isochrone to survive alongside.
	await page.mouse.click(...(await canvasPoint(-40, -30)));
	await page.mouse.click(...(await canvasPoint(40, 30)));
	await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeEnabled({
		timeout: 30_000
	});

	// Right-click somewhere off the route line to open the plain (non-marker)
	// context menu.
	await page.mouse.click(...(await canvasPoint(-100, 40)), { button: 'right' });
	await page.getByRole('menuitem', { name: 'Isochrone from here' }).click();

	const minutesInput = page.getByLabel('Minutes');
	await expect(minutesInput).toBeVisible();
	await minutesInput.fill('30');
	await page.getByRole('button', { name: 'Show isochrone' }).click();

	const hideButton = page.getByRole('button', { name: 'Hide isochrone' });
	await expect(hideButton).toBeVisible({ timeout: 30_000 });

	await hideButton.click();
	await expect(page.getByRole('button', { name: 'Hide isochrone' })).toHaveCount(0);

	// Show it again, then confirm Clear route takes both the waypoints and
	// the isochrone with it.
	await page.mouse.click(...(await canvasPoint(-100, 40)), { button: 'right' });
	await page.getByRole('menuitem', { name: 'Isochrone from here' }).click();
	await page.getByRole('button', { name: 'Show isochrone' }).click();
	await expect(page.getByRole('button', { name: 'Hide isochrone' })).toBeVisible({
		timeout: 30_000
	});

	await page.mouse.click(...(await canvasPoint(-100, 40)), { button: 'right' });
	await page.getByRole('menuitem', { name: 'Clear route' }).click();

	await expect(page.locator('.maplibregl-marker')).toHaveCount(0);
	await expect(page.getByRole('button', { name: 'Hide isochrone' })).toHaveCount(0);
});
