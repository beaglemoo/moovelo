import { expect, test } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { canRegister, NEEDS_REGISTRATION } from './support/auth';

// Changing the preset or the custom costing of an imported route re-routes it
// between its endpoints, discarding the imported track and its turn cues -
// exactly the consequence mayEdit() exists to confirm before. Every waypoint
// mutator goes through planEdit()/mayEdit(); changePreset and changeCustomCosting
// were the two that rerouted with no confirmation at all.

const password = 'e2e-preset-import-password-1';
const fixture = fileURLToPath(new URL('./fixtures/tring.gpx', import.meta.url));

test('changing the preset of an imported route confirms before discarding its track', async ({
	page
}) => {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(!canRegister(status), NEEDS_REGISTRATION);
	const email = `e2e-preset-import-${Date.now()}@example.com`;
	expect((await page.request.post('/api/auth/register', { data: { email, password } })).ok()).toBe(
		true
	);

	await page.goto('/library');
	await page.locator('input[type="file"]').setInputFiles(fixture);
	const row = page
		.locator('tbody tr')
		.filter({ has: page.getByRole('button', { name: 'E2E import fixture', exact: true }) });
	await expect(row).toHaveCount(1, { timeout: 60_000 });
	await expect(row).toContainText('imported');

	// Open it in the planner. It loads with source='imported'.
	await row.getByRole('button', { name: 'E2E import fixture', exact: true }).click();
	await expect(page).toHaveURL(/\/\?route=/);
	await expect(page.locator('.map canvas').first()).toBeVisible();

	// Collect every confirm dialog and accept it, so the reroute proceeds and
	// the test observes whether the warning was shown at all.
	const dialogs: string[] = [];
	page.on('dialog', (dialog) => {
		dialogs.push(dialog.message());
		void dialog.accept();
	});

	// Switch the preset - this re-routes the imported track, so it must warn
	// first, the same as an ordinary waypoint edit on this route would.
	await page.getByRole('radio', { name: 'Gravel' }).click();

	await expect
		.poll(() => dialogs.some((m) => m.includes('imported track')), { timeout: 10_000 })
		.toBe(true);
});
