import { expect, test } from '@playwright/test';

// The close button sits inside the draggable header. onHeaderPointerDown used
// to setPointerCapture on every pointerdown in that header, including one
// that started on the button - which retargets the resulting click to the
// header and the button's onclick never fires. Mouse and touch could not
// close an expanded card; only Tab+Enter worked, since keyboard activation
// never goes through pointer capture.

const password = 'e2e-assistant-close-password-1';

async function signIn(page: import('@playwright/test').Page) {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(
		!(status.setup_required || (status.signups_enabled && status.password_login)),
		'needs a fresh DB or SIGNUPS_ENABLED=true with password login'
	);
	const email = `e2e-assistant-close-${Date.now()}@example.com`;
	const registered = await page.request.post('/api/auth/register', { data: { email, password } });
	expect(registered.ok()).toBeTruthy();
}

async function enableAssistant(page: import('@playwright/test').Page) {
	await page.route('**/api/config', async (route) => {
		const response = await route.fetch();
		await route.fulfill({ json: { ...(await response.json()), assistant_enabled: true } });
	});
}

test('clicking the assistant close button collapses the card', async ({ page }) => {
	await signIn(page);
	await enableAssistant(page);
	await page.goto('/');
	const canvas = page.locator('.map canvas').first();
	await expect(canvas).toBeVisible();
	await page.waitForTimeout(2000);

	await page.getByRole('button', { name: 'Ask for a route' }).click();
	const header = page.locator('.assistant-head');
	await expect(header).toBeVisible();

	const closeButton = page.locator('.assistant-close');
	await closeButton.click();

	// Collapsing means the pill is back and the header is gone.
	await expect(page.getByRole('button', { name: 'Ask for a route' })).toBeVisible({
		timeout: 3000
	});
	await expect(header).not.toBeVisible();
});
