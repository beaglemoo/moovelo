import { expect, test } from '@playwright/test';

// Multi-step undo/redo (lib/history.svelte.ts): the planner's waypoint
// mutators push a snapshot of the inputs before mutating, so undo/redo can
// step back and forth through a whole editing session, not just pop the
// last waypoint.

const email = `e2e-undo-${Date.now()}@example.com`;
const password = 'e2e-undo-password-1';

test('multi-step undo and redo over waypoint edits', async ({ page }) => {
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

	const markers = page.locator('.maplibregl-marker');
	const undoBtn = page.getByRole('button', { name: 'Undo', exact: true });
	const redoBtn = page.getByRole('button', { name: 'Redo', exact: true });

	// The first click retries until it actually lands: MapView only attaches
	// its interaction handlers on the map's `load` event, so an early click
	// silently no-ops when tiles are still arriving.
	await expect(async () => {
		const box = (await canvas.boundingBox())!;
		await page.mouse.click(box.x + box.width / 2 - 60, box.y + box.height / 2 - 40);
		await expect(markers).toHaveCount(1, { timeout: 1_000 });
	}).toPass({ timeout: 30_000 });

	const box = (await canvas.boundingBox())!;
	const cx = box.x + box.width / 2;
	const cy = box.y + box.height / 2;
	await page.mouse.click(cx, cy);
	await page.mouse.click(cx + 60, cy + 40);
	await expect(markers).toHaveCount(3);
	await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeEnabled({
		timeout: 30_000
	});

	// Undo via the toolbar button, then via the keyboard shortcut.
	await expect(undoBtn).toBeEnabled();
	await undoBtn.click();
	await expect(markers).toHaveCount(2);

	await page.keyboard.press('ControlOrMeta+z');
	await expect(markers).toHaveCount(1);

	// Redo via the toolbar button, then via the keyboard shortcut.
	await expect(redoBtn).toBeEnabled();
	await redoBtn.click();
	await expect(markers).toHaveCount(2);

	await page.keyboard.press('ControlOrMeta+Shift+z');
	await expect(markers).toHaveCount(3);

	// A fresh edit after undo clears redo.
	await undoBtn.click();
	await expect(markers).toHaveCount(2);
	await page.mouse.click(cx + 90, cy - 70);
	await expect(markers).toHaveCount(3);
	await expect(redoBtn).toBeDisabled();

	// Clear is undoable.
	await page.getByRole('button', { name: 'Clear', exact: true }).click();
	await expect(markers).toHaveCount(0);
	await expect(undoBtn).toBeEnabled();
	await undoBtn.click();
	await expect(markers).toHaveCount(3);

	// Shortcuts are suppressed while typing in an input.
	await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeEnabled({
		timeout: 30_000
	});
	await page.getByRole('button', { name: 'Save', exact: true }).click();
	const nameInput = page.getByPlaceholder('Route name');
	await expect(nameInput).toBeVisible();
	await nameInput.focus();
	await page.keyboard.press('ControlOrMeta+z');
	await expect(markers).toHaveCount(3);
	await page.getByRole('button', { name: 'Cancel' }).click();

	// After undo, the Save button reflects dirty state again.
	await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeEnabled();
});
