import { expect, test } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { registerOrSkip, windowDropReady } from './support/auth';

// Seeds a route and a ride that followed it (same coordinates as
// activities.spec.ts's fixtures, imported in the order that lets
// services/route_match.py auto-match them on import), then exercises both
// new Phase 11 detail pages and the links between them. Runs against the dev
// compose stack and needs password registration - see support/auth.ts.

const password = 'e2e-planned-vs-actual-1';
const routeFile = fileURLToPath(new URL('./fixtures/tring.gpx', import.meta.url));
const rideFile = fileURLToPath(new URL('./fixtures/tring-ride.gpx', import.meta.url));

test('a matched ride and its route show planned-vs-actual on both detail pages', async ({
	page
}) => {
	await registerOrSkip(page, 'e2e-planned-vs-actual', password);

	// The route has to exist before the ride is imported: route matching
	// runs inline, once, at import time.
	await page.goto('/library');
	await windowDropReady(page);
	await page.locator('input[type="file"]').setInputFiles(routeFile);
	const routeResult = page.locator('.results li').first();
	await expect(routeResult).toContainText('turn cues', { timeout: 60_000 });

	await page.goto('/activities');
	await windowDropReady(page);
	await page.locator('input[type="file"]').setInputFiles(rideFile);
	const rideResult = page.locator('.results li').first();
	await expect(rideResult).toContainText('tring-ride.gpx', { timeout: 60_000 });

	const row = page.locator('tbody tr', { hasText: 'E2E ride fixture' });
	await expect(row).toBeVisible({ timeout: 60_000 });
	// The auto-match happened during import, so the route column is already
	// filled in by the time the row renders - not a later, separate step.
	const routeLink = row.locator('td[data-label="Route"] a');
	await expect(routeLink).toHaveText('E2E import fixture', { timeout: 30_000 });

	// The ride's own distance, as this list renders it. Used below to pin
	// which column of the comparison table is which: asserting only that the
	// rows are labelled Time/Distance/Ascent leaves the Actual and Planned
	// columns free to be swapped, and a swapped table still passed.
	const rideDistance = (await row.locator('td[data-label="Distance"]').innerText()).trim();

	// Ride detail: the comparison table and a working link to the route.
	await row.getByRole('link', { name: 'E2E ride fixture' }).click();
	await expect(page).toHaveURL(/\/activities\/[0-9a-f-]+$/);
	await expect(page.getByRole('heading', { name: 'E2E ride fixture' })).toBeVisible();
	const matchedRouteLink = page.locator('.route-link a');
	await expect(matchedRouteLink).toHaveText('E2E import fixture');
	const comparison = page.locator('table.comparison');
	await expect(comparison).toContainText('Time');
	await expect(comparison).toContainText('Distance');
	await expect(comparison).toContainText('Ascent');

	// Pin the columns, not just the row labels. The Actual cell must be the
	// ride's own distance - the same number and formatting the list showed -
	// and Planned must be the route's, which differs. Without both of these a
	// swapped Actual/Planned pair passes the spec unchanged.
	const distanceRow = comparison.locator('tr', { hasText: 'Distance' });
	const actualDistance = (await distanceRow.locator('td').nth(1).innerText()).trim();
	const plannedDistance = (await distanceRow.locator('td').nth(2).innerText()).trim();
	expect(actualDistance).toBe(rideDistance);
	// If the fixtures ever coincide, the assertion above stops discriminating
	// and this says so out loud rather than passing quietly.
	expect(plannedDistance).not.toBe(actualDistance);

	// Follow it to the route detail page, and back again.
	await matchedRouteLink.click();
	await expect(page).toHaveURL(/\/library\/[0-9a-f-]+$/);
	await expect(page.getByRole('heading', { name: 'E2E import fixture' })).toBeVisible();
	const rideLinkOnRoutePage = page.locator('.ride-name');
	await expect(rideLinkOnRoutePage).toHaveText('E2E ride fixture');
	await expect(page.locator('table')).toContainText('planned');

	await rideLinkOnRoutePage.click();
	await expect(page).toHaveURL(/\/activities\/[0-9a-f-]+$/);
	await expect(page.getByRole('heading', { name: 'E2E ride fixture' })).toBeVisible();
});

test('a failed route pick puts the picker back rather than mixing two routes', async ({ page }) => {
	await registerOrSkip(page, 'e2e-pick-fail', password);

	// TWO distinct routes: the picker needs something else to switch to, and
	// switching to the route already matched would prove nothing.
	await page.goto('/library');
	await windowDropReady(page);
	await page.locator('input[type="file"]').setInputFiles(routeFile);
	await expect(page.locator('.results li').first()).toContainText('turn cues', {
		timeout: 60_000
	});
	await page.locator('input[type="file"]').setInputFiles(rideFile);
	await expect(page.locator('.results li')).toHaveCount(2, { timeout: 60_000 });

	await page.goto('/activities');
	await windowDropReady(page);
	await page.locator('input[type="file"]').setInputFiles(rideFile);
	await expect(page.locator('.results li').first()).toContainText('tring-ride.gpx', {
		timeout: 60_000
	});

	const row = page.locator('tbody tr', { hasText: 'E2E ride fixture' });
	await expect(row).toBeVisible({ timeout: 60_000 });
	await row.getByRole('link', { name: 'E2E ride fixture' }).click();
	await expect(page).toHaveURL(/\/activities\/[0-9a-f-]+$/);

	const select = page.locator('.picker select');
	await expect(select.locator('option')).toHaveCount(3, { timeout: 20_000 });
	const before = await select.inputValue();
	const nameBefore = await page.locator('.route-link a').innerText();

	const values = await select
		.locator('option')
		.evaluateAll((nodes) => nodes.map((n) => (n as HTMLOptionElement).value));
	const other = values.find((v) => v && v !== before);
	expect(other, 'fixture must offer a second, different route').toBeTruthy();

	// The PUT lands; fetching the newly picked route then fails.
	await page.route('**/api/routes/*', (r) => r.abort());
	await select.selectOption(other!);
	await expect(page.locator('.picker-error, .error')).toBeVisible({ timeout: 10_000 });

	// The select is bound one way, so nothing re-renders it when the state it
	// reads is unchanged - without putting it back by hand it keeps showing the
	// route the rider picked while the panel below still describes the old one.
	// Getting the state right is necessary but not sufficient here, and this
	// assertion is the part that only a browser can make.
	expect(await select.inputValue()).toBe(before);
	await expect(page.locator('.route-link a')).toHaveText(nameBefore);
});

test('navigating between two route pages loads the second one', async ({ page }) => {
	await registerOrSkip(page, 'e2e-nav-swap', password);

	// Two routes, so there is somewhere to navigate to. SvelteKit reuses the
	// same +page.svelte between them - it is a same-route navigation, only the
	// dynamic segment changes - so a component that read its id once renders
	// the first route's content under the second's URL.
	await page.goto('/library');
	await windowDropReady(page);
	await page.locator('input[type="file"]').setInputFiles(routeFile);
	await expect(page.locator('.results li').first()).toContainText('turn cues', {
		timeout: 60_000
	});
	await page.locator('input[type="file"]').setInputFiles(rideFile);
	await expect(page.locator('.results li')).toHaveCount(2, { timeout: 60_000 });

	await page.goto('/library');
	await windowDropReady(page);
	const rows = page.locator('tbody tr');
	await expect(rows).toHaveCount(2, { timeout: 30_000 });
	// The row's name is a button (it opens the planner); the detail page is a
	// separate "Details" link.
	const firstName = (await rows.nth(0).locator('button.name').innerText()).trim();
	const secondName = (await rows.nth(1).locator('button.name').innerText()).trim();
	expect(firstName).not.toBe(secondName);

	const hrefs = await rows
		.locator('a', { hasText: 'Details' })
		.evaluateAll((nodes) => nodes.map((n) => (n as HTMLAnchorElement).getAttribute('href') ?? ''));
	expect(hrefs[0]).not.toBe(hrefs[1]);

	await rows.nth(0).getByRole('link', { name: 'Details' }).click();
	await expect(page).toHaveURL(/\/library\/[0-9a-f-]+$/);
	await expect(page.getByRole('heading', { name: firstName })).toBeVisible();

	// Straight from one detail page to the other, WITHOUT going via the list:
	// that is the navigation SvelteKit serves by reusing this component rather
	// than remounting it, and the only one that exercises a captured id.
	// Going back to the list first would unmount the page and hide the bug.
	await page.evaluate((href) => {
		const a = document.createElement('a');
		a.href = href;
		a.textContent = 'to the other route';
		a.id = 'e2e-sibling-link';
		document.body.appendChild(a);
	}, hrefs[1]);
	await page.locator('#e2e-sibling-link').click();

	await expect(page).toHaveURL(new RegExp(`${hrefs[1]}$`));
	await expect(page.getByRole('heading', { name: secondName })).toBeVisible({ timeout: 15_000 });
});
