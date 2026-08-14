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

	// Ride detail: the comparison table and a working link to the route.
	await row.getByRole('link', { name: 'E2E ride fixture' }).click();
	await expect(page).toHaveURL(/\/activities\/[0-9a-f-]+$/);
	await expect(page.getByRole('heading', { name: 'E2E ride fixture' })).toBeVisible();
	const matchedRouteLink = page.locator('.route-link a');
	await expect(matchedRouteLink).toHaveText('E2E import fixture');
	await expect(page.locator('table.comparison')).toContainText('Time');
	await expect(page.locator('table.comparison')).toContainText('Distance');
	await expect(page.locator('table.comparison')).toContainText('Ascent');

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
