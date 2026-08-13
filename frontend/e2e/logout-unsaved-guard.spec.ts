import { expect, test, type Page } from '@playwright/test';
import { canRegister, NEEDS_REGISTRATION } from './support/auth';

// Logging out with unsaved planner edits used to destroy the session before
// the guarded navigation ran, so cancelling the beforeNavigate confirm left a
// logged-out session behind a still-rendered planner - the nav bar gone, and
// a follow-up save silently 401ing. logout() now confirms first and only then
// tears the session down; dismissing the confirm leaves everything intact.

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

test('dismissing the log-out confirm keeps the session and the unsaved edit', async ({ page }) => {
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

	// Click Log out and dismiss the confirm. The session must be untouched.
	let asked = 0;
	page.once('dialog', (dialog) => {
		asked++;
		void dialog.dismiss();
	});
	await page.getByRole('button', { name: 'Log out' }).click();

	await expect.poll(() => asked, { timeout: 10_000 }).toBe(1);
	// Still logged in: the nav (rendered by {#if user}) is present, the session
	// is live, and the unsaved edit is still there to save.
	await expect(page).toHaveURL(/\/$/);
	await expect(page.getByRole('button', { name: 'Log out' })).toBeVisible();
	expect((await page.request.get('/api/auth/me')).status()).toBe(200);
	await expect(saveChanges).toBeVisible();
});
