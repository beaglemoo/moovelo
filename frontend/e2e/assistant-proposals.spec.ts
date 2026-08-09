import { expect, test, type Page } from '@playwright/test';

// Accepting what the assistant proposes.
//
// The stream is stubbed, but the route inside the proposal is a real one
// fetched from the running backend - so accepting it exercises the same
// snapshot shape Valhalla actually returns rather than a hand-written
// fixture that could drift away from it.

const password = 'e2e-proposal-password-1';
let accounts = 0;

async function signIn(page: Page) {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(
		!(status.setup_required || (status.signups_enabled && status.password_login)),
		'needs a fresh DB or SIGNUPS_ENABLED=true with password login'
	);
	const email = `e2e-proposal-${Date.now()}-${accounts++}@example.com`;
	const registered = await page.request.post('/api/auth/register', { data: { email, password } });
	expect(registered.ok()).toBeTruthy();
}

// Two points a few km apart in the Chilterns, inside the default England
// extract the dev stack builds tiles from.
const START = { lat: 51.7954, lon: -0.6583 };
const END = { lat: 51.7626, lon: -0.7442 };

async function realRoute(page: Page) {
	const response = await page.request.post('/api/route', {
		data: { waypoints: [START, END], preset: 'road', costing_options: null }
	});
	test.skip(!response.ok(), 'needs Valhalla routing tiles');
	return response.json();
}

async function enableAssistant(page: Page) {
	await page.route('**/api/config', async (route) => {
		const response = await route.fetch();
		await route.fulfill({ json: { ...(await response.json()), assistant_enabled: true } });
	});
}

function proposalStream(snapshot: unknown): string {
	return (
		`event: token\ndata: ${JSON.stringify({ text: 'Here is a route.' })}\n\n` +
		`event: proposal\ndata: ${JSON.stringify({
			waypoints: [START, END],
			preset: 'road',
			snapshot
		})}\n\n` +
		`event: done\ndata: ${JSON.stringify({ stopped_early: null, tools_called: ['plan_route'] })}\n\n`
	);
}

async function ask(page: Page, snapshot: unknown) {
	await page.route('**/api/assistant/chat/stream', async (route) => {
		await route.fulfill({
			status: 200,
			headers: { 'content-type': 'text/event-stream' },
			body: proposalStream(snapshot)
		});
	});
	await page.getByLabel('Ask the route assistant').fill('a route over the Chilterns');
	await page.getByRole('button', { name: 'Ask' }).click();
}

test('accept a proposed route and it becomes ordinary waypoints', async ({ page }) => {
	await signIn(page);
	const snapshot = await realRoute(page);
	await enableAssistant(page);

	await page.goto('/');
	await expect(page.locator('.map canvas').first()).toBeVisible();
	await page.waitForTimeout(2500);

	await ask(page, snapshot);

	// The card reports the snapshot's own figures, not the model's prose.
	const expectedKm = (snapshot.distance_m / 1000).toFixed(1);
	const accept = page.getByRole('button', { name: 'Use this route' });
	await expect(accept).toBeVisible();
	await expect(page.locator('.assistant-proposal')).toContainText(`${expectedKm} km`);

	// The ghost line is on the map while the offer stands.
	await expect
		.poll(() => page.evaluate(() => document.querySelectorAll('.maplibregl-marker').length))
		.toBe(0);

	await accept.click();

	// Accepting turns it into ordinary waypoints and an ordinary route.
	const markers = page.locator('.maplibregl-marker');
	await expect(markers).toHaveCount(2);
	await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeEnabled({
		timeout: 30_000
	});
	// The card is gone, and so is the offer.
	await expect(accept).toBeHidden();

	// Undo puts the planner back to having no route at all.
	await page.getByRole('button', { name: 'Undo', exact: true }).click();
	await expect(markers).toHaveCount(0);
});

test('editing the route discards a stale proposal', async ({ page }) => {
	await signIn(page);
	const snapshot = await realRoute(page);
	await enableAssistant(page);

	await page.goto('/');
	const canvas = page.locator('.map canvas').first();
	await expect(canvas).toBeVisible();
	await page.waitForTimeout(2500);

	await ask(page, snapshot);
	const accept = page.getByRole('button', { name: 'Use this route' });
	await expect(accept).toBeVisible();

	// Put a waypoint on the map: the rider has moved on, so the offer no
	// longer describes what they are looking at and must not stay clickable.
	await expect(async () => {
		const box = (await canvas.boundingBox())!;
		await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
		await expect(page.locator('.maplibregl-marker')).toHaveCount(1, { timeout: 1_000 });
	}).toPass({ timeout: 30_000 });

	await expect(accept).toBeHidden();
});
