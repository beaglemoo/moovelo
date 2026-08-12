import { expect, test } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { canRegister, NEEDS_REGISTRATION } from './support/auth';

// The personal heatmap toggle: hidden for a rider with nothing imported,
// shown and persisted once they have a ride. Runs against the dev compose
// stack and needs password registration to be possible - either a fresh
// database (first user) or SIGNUPS_ENABLED=true, the same precondition
// activities.spec.ts uses.

const email = `e2e-heatmap-${Date.now()}@example.com`;
const password = 'e2e-heatmap-password-1';
const ride = fileURLToPath(new URL('./fixtures/tring-ride.gpx', import.meta.url));

test('the heatmap toggle appears only once a ride is imported, and persists', async ({ page }) => {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(!canRegister(status), NEEDS_REGISTRATION);

	const registered = await page.request.post('/api/auth/register', { data: { email, password } });
	expect(registered.ok()).toBeTruthy();

	// Nothing imported yet - the toggle would only ever fetch empty tiles,
	// so it is left out of the map entirely rather than offered inert.
	await page.goto('/');
	const heatmapToggle = page.getByRole('button', { name: 'Heatmap' });
	await expect(heatmapToggle).not.toBeAttached();

	await page.goto('/activities');
	await page.locator('input[type="file"]').setInputFiles(ride);
	await expect(page.locator('.results li').first()).toContainText('tring-ride.gpx', {
		timeout: 60_000
	});

	await page.goto('/');
	await expect(heatmapToggle).toBeVisible({ timeout: 30_000 });
	await expect(heatmapToggle).toHaveAttribute('aria-pressed', 'false');

	await heatmapToggle.click();
	await expect(heatmapToggle).toHaveAttribute('aria-pressed', 'true');

	// Persisted independently of the cycle-network overlay's own toggle -
	// moovelo:heatmap, not moovelo:cycle-network - and survives a reload.
	await page.reload();
	await expect(page.getByRole('button', { name: 'Heatmap' })).toHaveAttribute(
		'aria-pressed',
		'true'
	);
});
