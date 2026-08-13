import { expect, test, type Page } from '@playwright/test';
import { canRegister, NEEDS_REGISTRATION } from './support/auth';

// Logging out with unsaved planner edits must prompt exactly once. logout()
// runs its own confirm, and the planner's beforeNavigate guard skips /login,
// so accepting proceeds cleanly to /login and dismissing returns with the
// session and edits intact. The earlier fix cleared unsaved.dirty, but the
// guard reads the planner's own local dirty state, so it fired a second (and
// third) prompt on the goto('/login') and re-stranded the session.

const password = 'e2e-logout-guard-password-1';
let accounts = 0;

async function signIn(page: Page) {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(!canRegister(status), NEEDS_REGISTRATION);
	const email = `e2e-logout-guard-${Date.now()}-${accounts++}@example.com`;
	expect((await page.request.post('/api/auth/register', { data: { email, password } })).ok()).toBe(
		true
	);
}

/** Sign in, plan and save a route, then make one unsaved edit so the planner
 * is dirty. Returns the "Save changes" locator that proves the dirty state. */
async function dirtySavedPlanner(page: Page) {
	await signIn(page);
	await page.goto('/');
	const canvas = page.locator('.map canvas').first();
	await expect(canvas).toBeVisible();
	await page.waitForTimeout(2500);

	const save = page.getByRole('button', { name: 'Save', exact: true });
	await expect(async () => {
		const box = (await canvas.boundingBox())!;
		await page.mouse.click(box.x + box.width / 2 - 40, box.y + box.height / 2 - 30);
		await page.mouse.click(box.x + box.width / 2 + 40, box.y + box.height / 2 + 30);
		await expect(save).toBeEnabled({ timeout: 8_000 });
	}).toPass({ timeout: 40_000 });

	await save.click();
	await page.getByPlaceholder('Route name').fill('E2E logout guard');
	await page.locator('.dialog').getByRole('button', { name: 'Save' }).click();
	await expect(page.getByRole('button', { name: 'Saved', exact: true })).toBeVisible({
		timeout: 30_000
	});

	const saveChanges = page.getByRole('button', { name: 'Save changes', exact: true });
	await expect(async () => {
		const box = (await canvas.boundingBox())!;
		await page.mouse.click(box.x + box.width * 0.5, box.y + box.height * 0.7);
		await expect(saveChanges).toBeVisible({ timeout: 3_000 });
	}).toPass({ timeout: 20_000 });
	return saveChanges;
}

test('dismissing the log-out confirm keeps the session and the unsaved edit', async ({ page }) => {
	const saveChanges = await dirtySavedPlanner(page);

	let asked = 0;
	page.on('dialog', (dialog) => {
		asked++;
		void dialog.dismiss();
	});
	await page.getByRole('button', { name: 'Log out' }).click();

	await expect.poll(() => asked, { timeout: 10_000 }).toBe(1);
	// Still logged in: nav present, session live, edit still saveable.
	await expect(page).toHaveURL(/\/$/);
	await expect(page.getByRole('button', { name: 'Log out' })).toBeVisible();
	expect((await page.request.get('/api/auth/me')).status()).toBe(200);
	await expect(saveChanges).toBeVisible();
});

test('confirming the log-out prompts exactly once and lands on /login', async ({ page }) => {
	await dirtySavedPlanner(page);

	// Accept every dialog. With the guard skipping /login there is exactly one
	// (logout's own); before the fix, logout()'s goto('/login') tripped the
	// planner's beforeNavigate guard for a second prompt (and the 401 recovery
	// a third).
	let asked = 0;
	page.on('dialog', (dialog) => {
		asked++;
		void dialog.accept();
	});
	await page.getByRole('button', { name: 'Log out' }).click();

	await expect(page).toHaveURL(/\/login$/);
	await expect(page.getByRole('button', { name: 'Log out' })).toBeHidden();
	expect((await page.request.get('/api/auth/me')).status()).toBe(401);
	// A short settle to catch any stray follow-on prompt the old code fired.
	await page.waitForTimeout(500);
	expect(asked).toBe(1);
});
