import { expect, test } from '@playwright/test';
import { canRegister, NEEDS_REGISTRATION } from './support/auth';

// Route alternates (Valhalla's own `alternates`, only ever valid for an
// exactly-2-waypoint A-to-B route): plan such a route, ask for alternatives,
// and - when Valhalla actually offers one for the clicked geometry - adopt
// it and confirm undo restores the previous stats exactly.
//
// Fewer than requested (or none at all) is normal, not a bug (see
// AlternatesQuery's docstring and route_alternates_real.json's own capture
// notes), so this spec is resilient to a real Valhalla simply not finding a
// distinct alternative for wherever the two random clicks landed.

const email = `e2e-alternates-${Date.now()}@example.com`;
const password = 'e2e-alternates-password-1';

test('alternate routes list, and adopting one is undoable', async ({ page }) => {
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
	// The first click retries until it actually lands: MapView only attaches
	// its interaction handlers on the map's `load` event, so an early click
	// silently no-ops when tiles are still arriving.
	await expect(async () => {
		const box = (await canvas.boundingBox())!;
		await page.mouse.click(box.x + box.width / 2 - 80, box.y + box.height / 2 - 60);
		await expect(markers).toHaveCount(1, { timeout: 1_000 });
	}).toPass({ timeout: 30_000 });

	const box = (await canvas.boundingBox())!;
	await page.mouse.click(box.x + box.width / 2 + 80, box.y + box.height / 2 + 60);
	await expect(markers).toHaveCount(2);

	const distanceStat = page.locator('.stats span').first();
	await expect(distanceStat).toContainText('km', { timeout: 30_000 });
	const beforeDistance = await distanceStat.textContent();

	const alternativesButton = page.getByRole('button', { name: /^Alternatives/, exact: false });
	await expect(alternativesButton).toBeEnabled();
	await alternativesButton.click();

	const panel = page.getByRole('dialog', { name: 'Alternative routes' });
	await expect(panel).toBeVisible();

	// Wait out the search: either "Use" rows appear, or the panel settles on
	// its own no-alternative note.
	const useButton = panel.getByRole('button', { name: 'Use' }).first();
	const noneNote = panel.getByText(/no distinct alternative/i);
	await expect(useButton.or(noneNote)).toBeVisible({ timeout: 60_000 });

	if (!(await useButton.isVisible())) {
		test.info().annotations.push({
			type: 'note',
			description: 'Valhalla returned no alternative for this pair of clicks - nothing to adopt.'
		});
		return;
	}

	await useButton.click();
	await expect(panel).not.toBeVisible();

	// Adopting an alternate is an ordinary route swap: the stats row still
	// shows a real distance (possibly unchanged, but never blank/NaN).
	await expect(distanceStat).toContainText('km');
	const adoptedDistance = await distanceStat.textContent();

	// Undo restores exactly the pre-adoption distance text - the whole point
	// of routeOverride: replaying reroute() would just refetch the primary,
	// which happens to be the same value, but the property under test is
	// that undo *works* for this kind of edit at all.
	const undoBtn = page.getByRole('button', { name: 'Undo', exact: true });
	await expect(undoBtn).toBeEnabled();
	await undoBtn.click();
	await expect(distanceStat).toHaveText(beforeDistance ?? '');

	// And redo restores the ADOPTED route, not a refetched primary - the
	// undo/redo entries carry the live route as routeOverride, so the
	// rider's pick survives a round trip through history.
	const redoBtn = page.getByRole('button', { name: 'Redo', exact: true });
	await expect(redoBtn).toBeEnabled();
	await redoBtn.click();
	await expect(distanceStat).toHaveText(adoptedDistance ?? '');
});
