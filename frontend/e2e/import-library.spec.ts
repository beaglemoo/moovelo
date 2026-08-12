import { expect, test } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { canRegister } from './support/auth';

// Imports a real GPX through the UI and exercises the library around it.
// Runs against the dev compose stack and needs password registration to be
// possible - either a fresh database (first user) or SIGNUPS_ENABLED=true.

const email = `e2e-import-${Date.now()}@example.com`;
const password = 'e2e-import-password-1';
const fixture = fileURLToPath(new URL('./fixtures/tring.gpx', import.meta.url));

test('import a GPX, then organise it in the library', async ({ page }) => {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(!canRegister(status), 'needs a fresh DB or SIGNUPS_ENABLED=true with password login');

	const registered = await page.request.post('/api/auth/register', {
		data: { email, password }
	});
	expect(registered.ok()).toBeTruthy();

	await page.goto('/library');

	// The file input is hidden behind the Import button by design.
	await page.locator('input[type="file"]').setInputFiles(fixture);

	// The result row reports what was actually imported.
	const result = page.locator('.results li').first();
	await expect(result).toContainText('tring.gpx', { timeout: 60_000 });
	await expect(result).toContainText('turn cues', { timeout: 60_000 });

	// The route must appear in the table itself, not just the results panel.
	// Matched on the exact name, so the "(copy)" made later does not also match.
	const row = page
		.locator('tbody tr')
		.filter({ has: page.getByRole('button', { name: 'E2E import fixture', exact: true }) });
	await expect(row).toHaveCount(1, { timeout: 30_000 });
	await expect(row).toContainText('imported');

	// Favourite it, then tag it.
	await row.locator('button.star').click();
	await expect(row.locator('button.star')).toHaveAttribute('aria-pressed', 'true');

	await row.getByRole('button', { name: '+ tag' }).click();
	await page.locator('.tag-input').fill('gravel, e2e');
	await page.locator('.tag-input').press('Enter');
	await expect(row.locator('.tag')).toHaveText(['gravel', 'e2e']);

	// Search finds it, and a term that matches nothing does not.
	await page.getByPlaceholder('Search names and notes').fill('E2E import');
	await expect(page.locator('tbody tr')).toHaveCount(1);
	await page.getByPlaceholder('Search names and notes').fill('zzzz-no-such-route');
	await expect(page.locator('.empty')).toContainText('Nothing matches');
	await page.getByRole('button', { name: 'Clear filters' }).click();

	// Duplicating and reversing both add a route rather than replacing one.
	const before = await page.locator('tbody tr').count();
	await row.getByRole('button', { name: 'Duplicate' }).click();
	await expect(page.locator('tbody tr')).toHaveCount(before + 1, { timeout: 30_000 });
	await expect(page.locator('tbody tr', { hasText: '(copy)' })).toHaveCount(1);

	await row.getByRole('button', { name: 'Reverse' }).click();
	await expect(page.locator('tbody tr', { hasText: '(reversed)' })).toHaveCount(1, {
		timeout: 60_000
	});
});
