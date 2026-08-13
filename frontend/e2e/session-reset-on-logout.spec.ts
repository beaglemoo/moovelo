import { expect, test, type Page } from '@playwright/test';
import { canRegister, NEEDS_REGISTRATION } from './support/auth';

// Session-scoped client state lives in module singletons (the planner's undo
// history, the import queues) so it survives navigation. A logout is a
// client-side navigation too, so without an explicit reset those singletons
// also survive it, and the next account to log in ON THE SAME TAB inherits the
// previous rider's undo stack and import results. Both are reproduced here by
// logging user A out and logging user B in without ever reloading the page -
// the register form's own client-side goto('/'), never page.goto, is what keeps
// the SPA (and its leaked singletons) alive between the two accounts.

const password = 'e2e-session-reset-password-1';
let accounts = 0;

async function registerViaApi(page: Page): Promise<void> {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(!canRegister(status), NEEDS_REGISTRATION);
	const email = `e2e-session-reset-${Date.now()}-${accounts++}@example.com`;
	expect((await page.request.post('/api/auth/register', { data: { email, password } })).ok()).toBe(
		true
	);
}

/** Log the second account in through the login page's own register form - a
 * client-side goto('/'), so the tab is never reloaded and any singleton A left
 * behind is still in memory. Assumes the page is already on /login (where a
 * UI log-out lands). */
async function registerNextViaForm(page: Page): Promise<void> {
	await expect(page).toHaveURL(/\/login$/);
	const email = `e2e-session-reset-${Date.now()}-${accounts++}@example.com`;
	await page.getByRole('button', { name: /Need an account\? Register/ }).click();
	await page.locator('input[type="email"]').fill(email);
	await page.locator('input[type="password"]').fill(password);
	await page.getByRole('button', { name: 'Create account', exact: true }).click();
	await expect(page).toHaveURL(/\/$/);
}

test('a fresh account cannot undo into the previous account_s route', async ({ page }) => {
	await registerViaApi(page);
	await page.goto('/');
	const canvas = page.locator('.map canvas').first();
	await expect(canvas).toBeVisible();
	await page.waitForTimeout(2500);

	// Two waypoints so the planner has something to undo (Undo enables).
	const undo = page.getByRole('button', { name: 'Undo', exact: true });
	await expect(async () => {
		const box = (await canvas.boundingBox())!;
		await page.mouse.click(box.x + box.width / 2 - 40, box.y + box.height / 2 - 30);
		await page.mouse.click(box.x + box.width / 2 + 40, box.y + box.height / 2 + 30);
		await expect(undo).toBeEnabled({ timeout: 8_000 });
	}).toPass({ timeout: 40_000 });

	// Log out (accept the unsaved-changes confirm), then log the next account in
	// without a reload.
	page.on('dialog', (dialog) => void dialog.accept());
	await page.getByRole('button', { name: 'Log out' }).click();
	await registerNextViaForm(page);

	// B has never touched the map: Undo must be disabled and no marker present.
	await expect(page.locator('.map canvas').first()).toBeVisible();
	await page.waitForTimeout(1500);
	await expect(
		page.getByRole('button', { name: 'Undo', exact: true }),
		"a fresh account must not inherit the previous account's undo stack"
	).toBeDisabled();
});

test('a fresh account does not see the previous account_s import results', async ({ page }) => {
	await registerViaApi(page);
	await page.goto('/library');
	await expect(page.getByRole('button', { name: 'Import', exact: true })).toBeVisible();

	// Upload a ride and leave the result row un-dismissed.
	await page.locator('input[type="file"]').setInputFiles('e2e/fixtures/tring.gpx');
	await expect(page.locator('.results li')).toHaveCount(1, { timeout: 30_000 });

	// Log out from the library (planner not mounted, nothing dirty, no confirm),
	// then log the next account in without a reload.
	await page.getByRole('button', { name: 'Log out' }).click();
	await registerNextViaForm(page);

	// Back to the library as B via a client-side nav (not page.goto).
	await page.getByRole('link', { name: 'Library' }).click();
	await expect(page).toHaveURL(/\/library$/);
	await expect(
		page.locator('.results li'),
		"a fresh account must not see the previous account's leftover import results"
	).toHaveCount(0);
});
