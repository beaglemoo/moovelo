import { expect, test, type Page } from '@playwright/test';
import { canRegister, NEEDS_REGISTRATION } from './support/auth';

// A logout while an import is still in flight bumps the queue's epoch and
// resets its counter. The logged-out user's straggler request, when it finally
// settles, must be inert - otherwise it drops the shared #running counter below
// the value the next account's own import expects, and busy is stranded true so
// the next account's Import button sticks on "Importing…" forever. Reproduced
// by holding user A's import request open, logging A out, and confirming user
// B's own (fast) import still returns the button to "Import".

const password = 'e2e-import-straggler-password-1';
let accounts = 0;

async function registerViaApi(page: Page): Promise<void> {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(!canRegister(status), NEEDS_REGISTRATION);
	const email = `e2e-import-straggler-${Date.now()}-${accounts++}@example.com`;
	expect((await page.request.post('/api/auth/register', { data: { email, password } })).ok()).toBe(
		true
	);
}

async function registerNextViaForm(page: Page): Promise<void> {
	await expect(page).toHaveURL(/\/login$/);
	const email = `e2e-import-straggler-${Date.now()}-${accounts++}@example.com`;
	await page.getByRole('button', { name: /Need an account\? Register/ }).click();
	await page.locator('input[type="email"]').fill(email);
	await page.locator('input[type="password"]').fill(password);
	await page.getByRole('button', { name: 'Create account', exact: true }).click();
	await expect(page).toHaveURL(/\/$/);
}

test('a logged-out user_s in-flight import does not strand the next user_s Import button', async ({
	page
}) => {
	// Gate imports: hold the first (user A's) open past the assertion window, let
	// every later one (user B's) resolve normally.
	let imports = 0;
	await page.route('**/api/routes/import', async (route) => {
		imports += 1;
		if (imports === 1) {
			await new Promise((r) => setTimeout(r, 12_000));
			await route.abort();
		} else {
			await route.continue();
		}
	});

	await registerViaApi(page);
	await page.goto('/library');
	await expect(page.locator('button.import')).toBeVisible();

	// A starts an import; its request is held, so the queue is genuinely busy.
	await page.locator('input[type="file"]').setInputFiles('e2e/fixtures/tring.gpx');
	await expect(page.locator('button.import')).toHaveText('Importing…', { timeout: 10_000 });

	// Log out mid-import (planner not mounted, nothing dirty, no confirm), then
	// log the next account in without a reload.
	await page.getByRole('button', { name: 'Log out' }).click();
	await registerNextViaForm(page);

	// Back to the library as B and import a file that resolves promptly.
	await page.getByRole('link', { name: 'Library' }).click();
	await expect(page).toHaveURL(/\/library$/);
	await page.locator('input[type="file"]').setInputFiles('e2e/fixtures/tring-ride.gpx');

	// B's own import finishes, so the button must return to "Import". Without the
	// epoch guard, A's straggler left #running at 1, B's finally dropped it to 1
	// (not 0), and busy stayed true - the button sticks on "Importing…".
	await expect(
		page.locator('button.import'),
		"the next account's Import button must not stay stuck on Importing…"
	).toHaveText('Import', { timeout: 10_000 });
});
