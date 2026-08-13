import { expect, test, type Page } from '@playwright/test';
import { canRegister, NEEDS_REGISTRATION } from './support/auth';

// Leaving the planner while it holds unsaved edits used to discard them with
// no warning: only the window file-drop path checked unsaved.dirty, so
// clicking the Library nav (or a library route, or an import result's View)
// navigated away and the ?route= loader overwrote the planner in silence.
// A beforeNavigate guard now confirms before any navigation away from a
// dirty planner - the same protection beforeunload already gives a tab close.

const password = 'e2e-unsaved-nav-password-1';
let accounts = 0;

async function planTwoWaypoints(page: Page) {
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
}

async function signIn(page: Page) {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(!canRegister(status), NEEDS_REGISTRATION);
	const email = `e2e-unsaved-nav-${Date.now()}-${accounts++}@example.com`;
	expect((await page.request.post('/api/auth/register', { data: { email, password } })).ok()).toBe(
		true
	);
}

test('leaving a dirty planner confirms before discarding unsaved edits', async ({ page }) => {
	await signIn(page);
	await page.goto('/');
	await planTwoWaypoints(page);

	// Save, then make an unsaved edit so the planner is dirty.
	await page.getByRole('button', { name: 'Save', exact: true }).click();
	await page.getByPlaceholder('Route name').fill('E2E unsaved nav');
	await page.locator('.dialog').getByRole('button', { name: 'Save' }).click();
	await expect(page.getByRole('button', { name: 'Saved', exact: true })).toBeVisible({
		timeout: 30_000
	});

	// Add a waypoint - the map shrinks once the stats/elevation panels open, so
	// the click offset is a fraction of the live box and retried until the
	// toolbar shows the unsaved-change state.
	const canvas = page.locator('.map canvas').first();
	const saveChanges = page.getByRole('button', { name: 'Save changes', exact: true });
	await expect(async () => {
		const box = (await canvas.boundingBox())!;
		await page.mouse.click(box.x + box.width * 0.5, box.y + box.height * 0.7);
		await expect(saveChanges).toBeVisible({ timeout: 3_000 });
	}).toPass({ timeout: 20_000 });

	// Clicking away must warn. Cancel it and confirm we stay put with the edit.
	let asked = 0;
	page.once('dialog', (dialog) => {
		asked++;
		void dialog.dismiss();
	});
	await page.getByRole('link', { name: 'Library' }).click();

	await expect.poll(() => asked, { timeout: 10_000 }).toBe(1);
	await expect(page).toHaveURL(/\/$/);
	await expect(saveChanges).toBeVisible();
});
