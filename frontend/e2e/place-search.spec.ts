import { expect, test } from '@playwright/test';

// Searches for a place and turns the result into a waypoint.
//
// Unlike the other specs this one gates on the place index rather than on
// auth: the indexer is opt-in, so a perfectly healthy instance may have no
// places at all, and there would be nothing to search for.

const email = `e2e-search-${Date.now()}@example.com`;
const password = 'e2e-search-password-1';

test('search for a place and route from it', async ({ page }) => {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(
		!(status.setup_required || (status.signups_enabled && status.password_login)),
		'needs a fresh DB or SIGNUPS_ENABLED=true with password login'
	);
	const config = await page.request.get('/api/config').then((r) => r.json());
	test.skip(
		!config.search_enabled,
		'needs the place index: docker compose --profile index run --rm indexer'
	);

	const registered = await page.request.post('/api/auth/register', {
		data: { email, password }
	});
	expect(registered.ok()).toBeTruthy();

	await page.goto('/');

	const box = page.getByRole('combobox');
	await expect(box).toBeVisible();

	// Two characters is the minimum; one must not even open the list.
	await box.fill('t');
	await expect(page.getByRole('listbox')).toBeHidden();

	await box.fill('tring');
	const options = page.getByRole('option');
	await expect(options.first()).toContainText('Tring', { timeout: 15_000 });

	// The town, not the hamlet or the railway station that share its name.
	await expect(options.first()).toContainText('town');

	// Arrow keys move the highlight, and it wraps at the ends.
	await expect(options.first()).toHaveAttribute('aria-selected', 'true');
	await box.press('ArrowDown');
	await expect(options.nth(1)).toHaveAttribute('aria-selected', 'true');
	await box.press('ArrowUp');
	await expect(options.first()).toHaveAttribute('aria-selected', 'true');

	// Escape closes without choosing anything.
	await box.press('Escape');
	await expect(page.getByRole('listbox')).toBeHidden();

	// Enter picks the highlighted result, which becomes a waypoint - so the
	// Clear button, disabled with no waypoints, becomes usable.
	await box.fill('tring');
	await expect(options.first()).toContainText('Tring', { timeout: 15_000 });
	await box.press('Enter');

	await expect(page.getByRole('button', { name: 'Clear' })).toBeEnabled();
	await expect(box).toHaveValue('');
});
