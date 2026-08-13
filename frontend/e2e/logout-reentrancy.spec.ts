import { expect, test, type Page } from '@playwright/test';
import { canRegister, NEEDS_REGISTRATION } from './support/auth';

// A re-click on Log out while auth.logout() is still in flight is the same
// intent, not a new one. Without a re-entrancy guard the confirm() (already
// answered) fires a second time for one action. Reproduced by widening the
// in-flight window with an injected delay on /api/auth/logout, then clicking
// again while it runs - the button is disabled and logout() early-returns, so
// exactly one confirm dialog appears.

const password = 'e2e-logout-reentrancy-password-1';
let accounts = 0;

async function dirtyPlanner(page: Page): Promise<void> {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(!canRegister(status), NEEDS_REGISTRATION);
	const email = `e2e-logout-reentrancy-${Date.now()}-${accounts++}@example.com`;
	expect((await page.request.post('/api/auth/register', { data: { email, password } })).ok()).toBe(
		true
	);
	await page.goto('/');
	const canvas = page.locator('.map canvas').first();
	await expect(canvas).toBeVisible();
	await page.waitForTimeout(2500);

	// Two waypoints so unsaved.dirty is true and the log-out confirm fires.
	const save = page.getByRole('button', { name: 'Save', exact: true });
	await expect(async () => {
		const box = (await canvas.boundingBox())!;
		await page.mouse.click(box.x + box.width / 2 - 40, box.y + box.height / 2 - 30);
		await page.mouse.click(box.x + box.width / 2 + 40, box.y + box.height / 2 + 30);
		await expect(save).toBeEnabled({ timeout: 8_000 });
	}).toPass({ timeout: 40_000 });
}

test('re-clicking Log out during a slow logout fires only one confirm', async ({ page }) => {
	await dirtyPlanner(page);

	// Widen the gap between confirm() resolving and auth.logout() resolving, so a
	// re-click reliably lands while the teardown is in flight.
	await page.route('**/api/auth/logout', async (route) => {
		await new Promise((r) => setTimeout(r, 800));
		await route.continue();
	});

	let asked = 0;
	page.on('dialog', (dialog) => {
		asked++;
		void dialog.accept();
	});

	const logoutButton = page.getByRole('button', { name: 'Log out' });
	await logoutButton.click();
	// Re-click during the 800ms in-flight window. force bypasses the disabled
	// actionability check so the attempt still happens; a disabled button fires
	// no onclick, and the loggingOut guard covers any timing gap - either way no
	// second confirm.
	await logoutButton.click({ force: true, timeout: 2_000 }).catch(() => {});

	await expect(page).toHaveURL(/\/login$/);
	await page.waitForTimeout(500);
	expect(asked, 'one log-out intent must produce exactly one confirm').toBe(1);
});
