import { expect, test } from '@playwright/test';
import { canRegister, NEEDS_REGISTRATION } from './support/auth';

// The theme system: prefers-color-scheme resolves the default, a manual
// override in the nav beats it in both directions, and the override persists
// across a reload with no flash of the wrong theme (app.html's inline
// FOUC-guard script). Runs against the dev compose stack like the other e2e
// specs.

const email = `e2e-dark-mode-${Date.now()}@example.com`;
const password = 'e2e-dark-mode-password-1';

/** html/body's own background - not the nav's, which is deliberately the
 * same dark chrome colour in both themes (see the token comment in
 * +layout.svelte) and so cannot distinguish light from dark. */
async function pageBackground(page: import('@playwright/test').Page): Promise<string> {
	return page.evaluate(() => getComputedStyle(document.body).backgroundColor);
}

test('dark mode follows the OS preference, and a manual override beats it and persists', async ({
	page
}) => {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(!canRegister(status), NEEDS_REGISTRATION);

	const registered = await page.request.post('/api/auth/register', {
		data: { email, password }
	});
	expect(registered.ok()).toBeTruthy();

	await page.emulateMedia({ colorScheme: 'dark' });
	await page.goto('/');
	await expect(page.getByRole('button', { name: 'Log out' })).toBeVisible();

	// Fresh account, no stored preference: 'system' mode follows the emulated
	// OS dark scheme. --bg's dark value is #002b36 = rgb(0, 43, 54).
	const themeToggle = page.locator('button.theme-toggle');
	await expect(themeToggle).toHaveText('Auto');
	await expect.poll(() => pageBackground(page)).toBe('rgb(0, 43, 54)');
	await expect(
		page.evaluate(() => document.documentElement.dataset.theme)
	).resolves.toBeUndefined();

	// One click cycles system -> light. The override must win even though the
	// OS is still reporting dark - --bg's light value is #fdf6e3 = rgb(253, 246, 227).
	await themeToggle.click();
	await expect(themeToggle).toHaveText('Light');
	await expect.poll(() => pageBackground(page)).toBe('rgb(253, 246, 227)');
	await expect(page.evaluate(() => document.documentElement.dataset.theme)).resolves.toBe('light');

	// The choice is a per-browser localStorage preference (like units): a
	// reload keeps it, and the FOUC-guard script in app.html applies it before
	// hydration, so there is nothing to wait for - the very first computed
	// style already reflects it.
	await page.reload();
	await expect(page.getByRole('button', { name: 'Log out' })).toBeVisible();
	await expect(page.locator('button.theme-toggle')).toHaveText('Light');
	await expect(await pageBackground(page)).toBe('rgb(253, 246, 227)');
	await expect(page.evaluate(() => document.documentElement.dataset.theme)).resolves.toBe('light');

	// One more click cycles light -> dark, an explicit override in the other
	// direction, still against an OS reporting dark throughout.
	await page.locator('button.theme-toggle').click();
	await expect(page.locator('button.theme-toggle')).toHaveText('Dark');
	await expect.poll(() => pageBackground(page)).toBe('rgb(0, 43, 54)');
	await expect(page.evaluate(() => document.documentElement.dataset.theme)).resolves.toBe('dark');
});

test('the served app.html carries the inline FOUC-guard script', async ({ page }) => {
	const response = await page.request.get('/');
	const body = await response.text();
	expect(body).toContain("localStorage.getItem('moovelo:theme')");
	expect(body).toContain('document.documentElement.dataset.theme');
});
