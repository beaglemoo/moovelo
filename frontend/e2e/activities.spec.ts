import { expect, test } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { canRegister, NEEDS_REGISTRATION } from './support/auth';

// Imports a recorded ride through the UI and exercises the activities page
// around it. Runs against the dev compose stack and needs password
// registration to be possible - either a fresh database (first user) or
// SIGNUPS_ENABLED=true.

const email = `e2e-activity-${Date.now()}@example.com`;
const password = 'e2e-activity-password-1';
const ride = fileURLToPath(new URL('./fixtures/tring-ride.gpx', import.meta.url));
const undated = fileURLToPath(new URL('./fixtures/tring.gpx', import.meta.url));

test('import a ride, see it listed, delete it', async ({ page }) => {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(!canRegister(status), NEEDS_REGISTRATION);

	const registered = await page.request.post('/api/auth/register', { data: { email, password } });
	expect(registered.ok()).toBeTruthy();

	await page.goto('/activities');

	// The empty state is a positive anchor: asserting on it first means a
	// later "the row is gone" cannot pass because the page never rendered.
	await expect(page.getByText('No rides yet')).toBeVisible();

	// The file input is hidden behind the Import button by design.
	await page.locator('input[type="file"]').setInputFiles(ride);

	const result = page.locator('.results li').first();
	await expect(result).toContainText('tring-ride.gpx', { timeout: 60_000 });
	// A ride reports distance and ascent and says nothing about turn cues -
	// it was never routed, so there are none to report.
	await expect(result).toContainText('m ascent', { timeout: 60_000 });
	await expect(result).not.toContainText('turn cues');

	const row = page.locator('tbody tr', { hasText: 'E2E ride fixture' });
	await expect(row).toBeVisible({ timeout: 60_000 });
	// Not the formatted string: the page renders in the viewer's locale, so
	// the day and month order is the browser's business, not the test's.
	await expect(row).toContainText('2026');
	// 15 points ten seconds apart, so moving time is a real number rather
	// than the "-" an untimed file would show.
	await expect(row.locator('td[data-label="Moving"]')).not.toHaveText('-');

	page.once('dialog', (dialog) => dialog.accept());
	await row.getByRole('button', { name: 'Delete' }).click();
	await expect(page.getByText('No rides yet')).toBeVisible({ timeout: 30_000 });
});

test('a ride with no timestamps is dated by when it was imported', async ({ page }) => {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(
		!(status.signups_enabled && status.password_login),
		'needs SIGNUPS_ENABLED=true with password login'
	);

	const registered = await page.request.post('/api/auth/register', {
		data: { email: `e2e-undated-${Date.now()}@example.com`, password }
	});
	expect(registered.ok()).toBeTruthy();

	await page.goto('/activities');
	await page.locator('input[type="file"]').setInputFiles(undated);

	// The point of the fallback: an undated ride still appears, labelled for
	// what it is, rather than showing an empty date that reads as a bug.
	const row = page.locator('tbody tr', { hasText: 'E2E import fixture' });
	await expect(row).toBeVisible({ timeout: 60_000 });
	await expect(row).toContainText('imported');
});
