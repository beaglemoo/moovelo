import { expect, test } from '@playwright/test';

// The chat panel over a stubbed stream.
//
// The SSE frames are served by page.route rather than by a real model: the
// point here is that the browser parses the stream and paints it as it
// arrives, and pinning that to a live endpoint would make the spec cost
// money, need a key, and fail for reasons that have nothing to do with the
// frontend. The wire contract itself is pinned server-side in
// backend/tests/test_assistant_stream.py.

const password = 'e2e-assistant-password-1';
// One account per test: Playwright gives each test its own context, so a
// session cannot be shared, and re-registering the same address fails.
let accounts = 0;

function sse(frames: [string, unknown][]): string {
	return frames
		.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)
		.join('');
}

async function signIn(page: import('@playwright/test').Page) {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(
		!(status.setup_required || (status.signups_enabled && status.password_login)),
		'needs a fresh DB or SIGNUPS_ENABLED=true with password login'
	);
	const email = `e2e-assistant-${Date.now()}-${accounts++}@example.com`;
	const registered = await page.request.post('/api/auth/register', { data: { email, password } });
	expect(registered.ok()).toBeTruthy();
}

test('ask the assistant and watch the reply stream in', async ({ page }) => {
	await signIn(page);

	// Force the feature on regardless of how this instance is configured, so
	// the spec covers the panel rather than the operator's env file.
	await page.route('**/api/config', async (route) => {
		const response = await route.fetch();
		const config = await response.json();
		await route.fulfill({ json: { ...config, assistant_enabled: true } });
	});

	await page.route('**/api/assistant/chat/stream', async (route) => {
		await route.fulfill({
			status: 200,
			headers: { 'content-type': 'text/event-stream' },
			body: sse([
				['tool_call', { name: 'search_place' }],
				['tool_result', { name: 'search_place', error: null }],
				['token', { text: 'Tring ' }],
				['token', { text: 'to Wendover, ' }],
				['token', { text: '18.4 km.' }],
				['done', { stopped_early: null, tools_called: ['search_place'] }]
			])
		});
	});

	await page.goto('/');

	// Collapsed to a pill by default - expand it before the input exists.
	await page.getByRole('button', { name: 'Ask for a route' }).click();

	const ask = page.getByLabel('Ask the route assistant');
	await expect(ask).toBeVisible();

	await ask.fill('how far is Tring to Wendover');
	await page.getByRole('button', { name: 'Ask' }).click();

	const log = page.getByRole('log', { name: 'Assistant conversation' });
	await expect(log).toContainText('how far is Tring to Wendover');
	// The three token frames land as one continuous reply.
	await expect(log).toContainText('Tring to Wendover, 18.4 km.');
	// And the turn is over: no stop button, no lingering status line.
	await expect(page.getByRole('button', { name: 'Ask' })).toBeVisible();
	await expect(page.getByRole('button', { name: 'Stop' })).toBeHidden();
});

test('the docked panel never appears just to hold the assistant', async ({ page }) => {
	await signIn(page);

	// Force the feature on: this is the case the docked-panel band used to
	// render for, all by itself, with no route drawn at all.
	await page.route('**/api/config', async (route) => {
		const response = await route.fetch();
		const config = await response.json();
		await route.fulfill({ json: { ...config, assistant_enabled: true } });
	});

	await page.goto('/');
	// The assistant lives as a floating overlay in .map-area now, so the
	// pill is visible with no route...
	await expect(page.getByRole('button', { name: 'Ask for a route' })).toBeVisible();
	// ...and the docked panel below the map - which used to render a
	// full-width band just to hold it - does not exist at all.
	await expect(page.locator('.panel')).toHaveCount(0);
});

test('the panel is absent when the assistant is not configured', async ({ page }) => {
	await signIn(page);

	await page.route('**/api/config', async (route) => {
		const response = await route.fetch();
		const config = await response.json();
		await route.fulfill({ json: { ...config, assistant_enabled: false } });
	});

	const configLoaded = page.waitForResponse('**/api/config');
	await page.goto('/');
	await configLoaded;
	// Wait for the planner to actually be up before asserting an absence:
	// toBeHidden() against a page that has not rendered yet passes for the
	// wrong reason, and did - removing the gate left this test green until
	// the wait was added.
	await expect(page.locator('canvas').first()).toBeVisible();
	// Neither the collapsed pill nor an expanded input exists.
	await expect(page.getByRole('button', { name: 'Ask for a route' })).toHaveCount(0);
	await expect(page.getByLabel('Ask the route assistant')).toHaveCount(0);
});
