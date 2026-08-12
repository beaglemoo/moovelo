import { expect, test } from '@playwright/test';
import { canRegister, NEEDS_REGISTRATION } from './support/auth';

// Gradient colouring on the elevation chart. Frontend-only: plans a real
// route so the ~500-point elevation array has genuine (unsmoothed)
// point-to-point noise, then checks the chart drew more than one gradient
// band. The map line shares the same segments and colour expression - not
// asserted here, since canvas pixels aren't a reliable thing to check - so
// this spec stands in for both.

const email = `e2e-gradient-${Date.now()}@example.com`;
const password = 'e2e-gradient-password-1';

test('elevation chart colours the route by gradient band', async ({ page }) => {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(!canRegister(status), NEEDS_REGISTRATION);

	const registered = await page.request.post('/api/auth/register', {
		data: { email, password }
	});
	expect(registered.ok()).toBeTruthy();

	await page.goto('/');
	const canvas = page.locator('.map canvas').first();
	await expect(canvas).toBeVisible();
	// Let the map settle before clicking waypoints onto it.
	await page.waitForTimeout(2500);

	// The default view is all of England at zoom 6.3, so even small pixel
	// offsets around the centre span a long inland leg with real elevation
	// variation - while larger fractions of the canvas land in the sea and
	// route to nothing. Same coordinates as smoke.spec.ts.
	const box = (await canvas.boundingBox())!;
	const cx = box.x + box.width / 2;
	const cy = box.y + box.height / 2;
	await page.mouse.click(cx - 40, cy - 30);
	await page.mouse.click(cx + 40, cy + 30);

	// Routing succeeded once Save lights up and the stats panel appears.
	const save = page.getByRole('button', { name: 'Save', exact: true });
	await expect(save).toBeEnabled({ timeout: 30_000 });

	const chart = page.locator('svg[aria-label="Elevation profile"]');
	await expect(chart).toBeVisible();

	const strokes = await chart
		.locator('path.line')
		.evaluateAll((nodes) => nodes.map((n) => n.getAttribute('stroke')));
	expect(strokes.length).toBeGreaterThan(0);
	expect(new Set(strokes).size).toBeGreaterThan(1);

	// The legend only lists bands the route actually has, and matches what
	// the chart drew.
	const legendDots = await page.locator('.legend .legend-dot').count();
	expect(legendDots).toBe(new Set(strokes).size);
});
