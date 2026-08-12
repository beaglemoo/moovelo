import { expect, test } from '@playwright/test';
import { canRegister, NEEDS_REGISTRATION } from './support/auth';

// Custom costing (Phase 8 PR1): the sliders popover routes with a custom
// bundle, the Custom pill reflects it, and a saved preset can be re-applied.

const email = `e2e-custom-${Date.now()}@example.com`;
const password = 'e2e-custom-password-1';

async function planASampleRoute(page: import('@playwright/test').Page) {
	const canvas = page.locator('.map canvas').first();
	await expect(canvas).toBeVisible();
	// Let the map settle before clicking waypoints onto it.
	await page.waitForTimeout(2500);

	const box = (await canvas.boundingBox())!;
	const cx = box.x + box.width / 2;
	const cy = box.y + box.height / 2;
	await page.mouse.click(cx - 40, cy - 30);
	await page.mouse.click(cx + 40, cy + 30);

	await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeEnabled({
		timeout: 30_000
	});
}

test('custom costing routes, saves as a preset, and re-applies', async ({ page }) => {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(!canRegister(status), NEEDS_REGISTRATION);

	const registered = await page.request.post('/api/auth/register', {
		data: { email, password }
	});
	expect(registered.ok()).toBeTruthy();

	await page.goto('/');
	await planASampleRoute(page);

	// Open the sliders popover and apply a custom bundle by changing the
	// bike type - `onchange` applies immediately.
	const customPill = page.getByRole('radio', { name: /Custom/ });
	await customPill.click();
	const popover = page.getByRole('dialog', { name: 'Custom costing options' });
	await expect(popover).toBeVisible();
	await popover.getByLabel('Bike type').selectOption('Mountain');

	// Applying custom costing reroutes; the Custom pill is now the active one.
	await expect(customPill).toHaveAttribute('aria-checked', 'true');
	await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeEnabled({
		timeout: 30_000
	});

	// Save the bundle as a named preset; it appears in the list as applied.
	await popover.getByLabel('New preset name').fill('E2E mountain');
	await popover.getByRole('button', { name: 'Save as preset' }).click();
	await expect(popover.getByRole('button', { name: 'E2E mountain', exact: true })).toBeVisible();

	// Picking a named preset clears custom mode.
	await page.getByRole('radio', { name: 'Road' }).click();
	await expect(customPill).toHaveAttribute('aria-checked', 'false');
	await expect(page.getByRole('radio', { name: 'Road' })).toHaveAttribute('aria-checked', 'true');

	// Re-applying the saved preset from the popover restores custom mode.
	await customPill.click();
	await popover.getByRole('button', { name: 'E2E mountain', exact: true }).click();
	await expect(customPill).toHaveAttribute('aria-checked', 'true');

	// Delete the preset so a re-run does not collide on the unique name.
	await popover.getByLabel('Delete preset E2E mountain').click();
	await expect(popover.getByText('No saved presets yet.')).toBeVisible();
});
