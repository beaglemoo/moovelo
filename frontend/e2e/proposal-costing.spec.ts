import { expect, test, type Page } from '@playwright/test';
import { canRegister, NEEDS_REGISTRATION } from './support/auth';

// Accepting an assistant proposal must not leave a stale custom-costing
// bundle behind. The proposal carries a named preset (road/gravel/quiet) and
// its geometry was routed under that preset; if customCostingOptions - set
// earlier in the session - survives the accept, then storedPreset computes
// 'custom' and the saved route persists preset='custom' plus an unrelated
// costing bundle against road-preset geometry, which a later reverse or
// recompute then mis-costs.

const password = 'e2e-proposal-costing-password-1';
let accounts = 0;

const START = { lat: 51.7954, lon: -0.6583 };
const END = { lat: 51.7626, lon: -0.7442 };

async function signIn(page: Page) {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(!canRegister(status), NEEDS_REGISTRATION);
	const email = `e2e-proposal-costing-${Date.now()}-${accounts++}@example.com`;
	const registered = await page.request.post('/api/auth/register', { data: { email, password } });
	expect(registered.ok()).toBeTruthy();
}

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

async function ask(page: Page, snapshot: unknown) {
	await page.route('**/api/assistant/chat/stream', async (route) => {
		await route.fulfill({
			status: 200,
			headers: { 'content-type': 'text/event-stream' },
			body:
				`event: token\ndata: ${JSON.stringify({ text: 'Here is a route.' })}\n\n` +
				`event: proposal\ndata: ${JSON.stringify({ waypoints: [START, END], preset: 'road', snapshot })}\n\n` +
				`event: done\ndata: ${JSON.stringify({ stopped_early: null, tools_called: ['plan_route'] })}\n\n`
		});
	});
	await page.getByRole('button', { name: 'Ask for a route' }).click();
	await page.getByLabel('Ask the route assistant').fill('a route over the Chilterns');
	await page.getByRole('button', { name: 'Ask' }).click();
}

test('accepting a proposal clears custom costing so the save matches the routed preset', async ({
	page
}) => {
	await signIn(page);
	const snapshot = await realRoute(page);
	await enableAssistant(page);

	await page.goto('/');
	const canvas = page.locator('.map canvas').first();
	await expect(canvas).toBeVisible();
	await page.waitForTimeout(2500);

	// Plan a route, then dial in a custom costing bundle (Mountain) - the
	// state a rider is in before asking the assistant for a suggestion. The
	// clicks are retried as a unit: a single pair can land before the map has
	// settled and route to nothing, the same flake the sibling proposal spec
	// guards against.
	const save = page.getByRole('button', { name: 'Save', exact: true });
	await expect(async () => {
		const box = (await canvas.boundingBox())!;
		await page.mouse.click(box.x + box.width / 2 - 40, box.y + box.height / 2 - 30);
		await page.mouse.click(box.x + box.width / 2 + 40, box.y + box.height / 2 + 30);
		await expect(save).toBeEnabled({ timeout: 8_000 });
	}).toPass({ timeout: 40_000 });

	const customPill = page.getByRole('radio', { name: /Custom/ });
	await customPill.click();
	const popover = page.getByRole('dialog', { name: 'Custom costing options' });
	await popover.getByLabel('Bike type').selectOption('Mountain');
	await expect(customPill).toHaveAttribute('aria-checked', 'true');
	await expect(save).toBeEnabled({ timeout: 30_000 });
	// Close the popover so it does not sit over the assistant controls.
	await page.keyboard.press('Escape');

	// Ask the assistant and accept its road-preset proposal.
	await ask(page, snapshot);
	const accept = page.getByRole('button', { name: 'Use this route' });
	await accept.click();
	await expect(page.locator('.maplibregl-marker')).toHaveCount(2);
	await expect(save).toBeEnabled({ timeout: 30_000 });

	// The Custom pill must no longer be active - the accepted route was routed
	// under the proposal's named preset, not the earlier custom bundle.
	await expect(customPill).toHaveAttribute('aria-checked', 'false');

	// Save, and capture what the route is actually persisted as.
	const saved = page.waitForRequest(
		(r) => r.url().endsWith('/api/routes') && r.method() === 'POST'
	);
	await save.click();
	await page.getByPlaceholder('Route name').fill('E2E proposal costing');
	await page.locator('.dialog').getByRole('button', { name: 'Save' }).click();
	const body = (await saved).postDataJSON();

	// The proposal's preset, with no stale custom bundle - not preset:'custom'
	// carrying the Mountain options the rider set before the proposal existed.
	expect(body.preset).toBe('road');
	expect(body.costing_options).toBeNull();
});
