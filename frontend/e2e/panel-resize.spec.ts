import { expect, test } from '@playwright/test';
import { canRegister, NEEDS_REGISTRATION } from './support/auth';

// The bottom route-details panel is drag-resizable and collapsible
// (lib/panel.svelte.ts) so the map keeps room for placing waypoints. Collapse
// hides everything but the handle and the stats line; the chosen size persists
// per browser in localStorage (moovelo:panel-height / moovelo:panel-collapsed).

const email = `e2e-panel-${Date.now()}@example.com`;
const password = 'e2e-panel-password-1';

test('collapse hides the details, expand restores, size persists', async ({ page }) => {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(!canRegister(status), NEEDS_REGISTRATION);

	const registered = await page.request.post('/api/auth/register', {
		data: { email, password }
	});
	expect(registered.ok()).toBeTruthy();

	await page.goto('/');
	const canvas = page.locator('.map canvas').first();
	await expect(canvas).toBeVisible();
	await page.waitForTimeout(2500);

	const markers = page.locator('.maplibregl-marker');
	const clickAt = async (fx: number, fy: number) => {
		const live = (await canvas.boundingBox())!;
		await page.mouse.click(live.x + live.width * fx, live.y + live.height * fy);
	};

	// Two waypoints make a route, which mounts the panel.
	await expect(async () => {
		await clickAt(0.35, 0.5);
		await expect(markers).toHaveCount(1, { timeout: 1_000 });
	}).toPass({ timeout: 30_000 });
	await clickAt(0.6, 0.55);
	await expect(markers).toHaveCount(2);

	const panel = page.locator('.panel');
	const waypointsHeading = page.getByRole('heading', { name: 'Waypoints' });
	await expect(panel).toBeVisible();
	await expect(waypointsHeading).toBeVisible();

	// Collapse: the details vanish, the panel shrinks, the stats stay.
	const collapse = page.getByRole('button', { name: 'Collapse route details' });
	const openHeight = (await panel.boundingBox())!.height;
	await collapse.click();
	await expect(waypointsHeading).toBeHidden();
	const collapsedHeight = (await panel.boundingBox())!.height;
	expect(collapsedHeight).toBeLessThan(openHeight);

	// Expand restores the details.
	await page.getByRole('button', { name: 'Expand route details' }).click();
	await expect(waypointsHeading).toBeVisible();

	// Drag the handle down to shrink the panel (giving the map more room -
	// the point of the feature), then confirm the size persists. Dragging the
	// other way is clamped near the default on a short viewport, since the map
	// keeps its 260px floor, so this is the reliable, representative direction.
	const handle = page.getByRole('separator', { name: 'Resize route details panel' });
	const before = (await panel.boundingBox())!.height;
	const hb = (await handle.boundingBox())!;
	await page.mouse.move(hb.x + hb.width / 2, hb.y + hb.height / 2);
	await page.mouse.down();
	await page.mouse.move(hb.x + hb.width / 2, hb.y + 150, { steps: 8 });
	await page.mouse.up();
	const after = (await panel.boundingBox())!.height;
	expect(after).toBeLessThan(before - 40);

	// The dragged height survives a reload (localStorage, not session state).
	await page.reload();
	await expect(canvas).toBeVisible();
	await page.waitForTimeout(2500);
	await expect(async () => {
		await clickAt(0.35, 0.5);
		await expect(markers).toHaveCount(1, { timeout: 1_000 });
	}).toPass({ timeout: 30_000 });
	await clickAt(0.6, 0.55);
	await expect(markers).toHaveCount(2);
	const reloaded = (await page.locator('.panel').boundingBox())!.height;
	expect(reloaded).toBeLessThan(before - 40);
});
