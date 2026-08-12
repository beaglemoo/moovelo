import { expect, test } from '@playwright/test';
import { canRegister, NEEDS_REGISTRATION } from './support/auth';

// POIs along a planned route, and the hover sync between the list and the
// map. Gated on the place index like the search spec: the indexer is
// opt-in, so a healthy instance may have no POIs at all.

const email = `e2e-pois-${Date.now()}@example.com`;
const password = 'e2e-pois-password-1';

test('find POIs on a route and sync hover with the map', async ({ page }) => {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(!canRegister(status), NEEDS_REGISTRATION);
	const config = await page.request.get('/api/config').then((r) => r.json());
	test.skip(
		!config.search_enabled,
		'needs the place index: docker compose --profile index run --rm indexer'
	);

	expect((await page.request.post('/api/auth/register', { data: { email, password } })).ok()).toBe(
		true
	);

	await page.goto('/');

	// Plan a short real ride so there is something to find alongside it.
	const box = page.getByRole('combobox');
	await box.fill('tring');
	await expect(page.getByRole('option').first()).toContainText('Tring', { timeout: 15_000 });
	await page.getByRole('option').first().getByRole('button', { name: 'From' }).click();
	await box.fill('wendover');
	await expect(page.getByRole('option').first()).toContainText('Wendover', { timeout: 15_000 });
	await page.getByRole('option').first().getByRole('button', { name: 'To', exact: true }).click();

	const chips = page.locator('.chips');
	const rows = page.locator('.pois li button');
	await expect(rows.first()).toBeVisible({ timeout: 30_000 });

	// Ordered along the ride, not by distance from it.
	const along = await rows.evaluateAll((nodes) =>
		nodes.map((n) => parseFloat(n.querySelector('.at')?.textContent ?? '0'))
	);
	expect(along).toEqual([...along].sort((a, b) => a - b));

	// Turning a category on brings more back; turning them all off says so
	// rather than showing an empty list that looks like "nothing here".
	const before = await rows.count();
	await chips.getByRole('button', { name: 'Pub' }).click();
	await expect(rows).not.toHaveCount(before, { timeout: 30_000 });

	for (const label of ['Water', 'Coffee', 'Toilets', 'Bike', 'Pub']) {
		const chip = chips.getByRole('button', { name: label, exact: true });
		if ((await chip.getAttribute('aria-pressed')) === 'true') await chip.click();
	}
	await expect(page.locator('.pois .note')).toContainText('Pick a category');

	// Back on, then check hovering a row marks it - the state the map layer
	// reads to enlarge the matching circle.
	await chips.getByRole('button', { name: 'Coffee', exact: true }).click();
	await expect(rows.first()).toBeVisible({ timeout: 30_000 });
	await rows.first().hover();
	await expect(rows.first()).toHaveClass(/hovered/);
});
