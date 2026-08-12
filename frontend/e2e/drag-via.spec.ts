import { expect, test } from '@playwright/test';
import { canRegister, NEEDS_REGISTRATION } from './support/auth';

// Drag-to-reroute: grabbing the route line and dropping it somewhere else
// inserts a via waypoint there and re-plans. Part of the core loop, and until
// now pinned by no spec at all.
//
// The case that matters here is where the drop lands. The map's own mouseup
// only fires for a release over its canvas container, and floating controls
// sit on top of that canvas - the zoom buttons, the basemap switch, the
// toolbar. Releasing over one of them used to drop the gesture in silence:
// no via, no reroute, and the ghost drag point left behind on the map.

const password = 'e2e-drag-password-1';

test.describe('drag the route line to insert a via', () => {
	test.beforeEach(async ({ page }) => {
		const status = await page.request.get('/api/auth/status').then((r) => r.json());
		test.skip(!canRegister(status), NEEDS_REGISTRATION);
		const registered = await page.request.post('/api/auth/register', {
			data: { email: `e2e-drag-${Date.now()}-${Math.random()}@example.com`, password }
		});
		expect(registered.ok()).toBeTruthy();
	});

	/**
	 * Plans a two-waypoint route, then returns a point that is provably on the
	 * route line: the map sets the canvas cursor to `grab` only while the
	 * pointer is over the `route-hit` layer, so walking the diagonal between
	 * the two clicks and reading the computed cursor finds the line wherever
	 * Valhalla actually put it. Guessing a point and hoping is how an earlier
	 * probe of this same gesture produced an inconclusive answer twice.
	 */
	async function planAndGrab(page: import('@playwright/test').Page) {
		const canvas = page.locator('.map canvas').first();
		await expect(canvas).toBeVisible();
		await page.waitForTimeout(2500);

		const canvasPoint = async (dx: number, dy: number): Promise<[number, number]> => {
			const box = (await canvas.boundingBox())!;
			return [box.x + box.width / 2 + dx, box.y + box.height / 2 + dy];
		};

		const markers = page.locator('.maplibregl-marker');
		await expect(async () => {
			await page.mouse.click(...(await canvasPoint(-40, -30)));
			await expect(markers).toHaveCount(1, { timeout: 1_000 });
		}).toPass({ timeout: 30_000 });
		await page.mouse.click(...(await canvasPoint(40, 30)));
		await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeEnabled({
			timeout: 30_000
		});

		const box = (await canvas.boundingBox())!;
		const cx = box.x + box.width / 2;
		const cy = box.y + box.height / 2;
		for (let t = 0.1; t <= 0.9; t += 0.03) {
			const x = cx - 40 + 80 * t;
			const y = cy - 30 + 60 * t;
			await page.mouse.move(x, y);
			await page.waitForTimeout(80);
			if ((await canvas.evaluate((el) => getComputedStyle(el).cursor)) === 'grab') {
				return { markers, grab: [x, y] as [number, number] };
			}
		}
		throw new Error('never found a point on the route line');
	}

	async function dragFromTo(
		page: import('@playwright/test').Page,
		from: [number, number],
		to: [number, number]
	) {
		await page.mouse.move(...from);
		await page.mouse.down();
		for (let i = 1; i <= 10; i++) {
			await page.mouse.move(
				from[0] + ((to[0] - from[0]) * i) / 10,
				from[1] + ((to[1] - from[1]) * i) / 10
			);
			await page.waitForTimeout(40);
		}
		await page.mouse.up();
	}

	test('dropping on the map inserts a via', async ({ page }) => {
		await page.goto('/');
		const { markers, grab } = await planAndGrab(page);
		await dragFromTo(page, grab, [grab[0] - 50, grab[1] + 40]);
		await expect(markers).toHaveCount(3, { timeout: 30_000 });
	});

	test('dropping on a control that overlays the map still inserts a via', async ({ page }) => {
		await page.goto('/');
		const { markers, grab } = await planAndGrab(page);

		// The zoom-in button is a real control floating over the canvas, so a
		// release on it never reaches the map's own mouseup.
		const zoomIn = page.locator('.maplibregl-ctrl-zoom-in');
		const zoomBox = (await zoomIn.boundingBox())!;
		await dragFromTo(page, grab, [zoomBox.x + zoomBox.width / 2, zoomBox.y + zoomBox.height / 2]);

		await expect(markers).toHaveCount(3, { timeout: 30_000 });
		// And the ghost point the drag draws is cleaned up rather than stranded.
		await expect(page.getByRole('button', { name: 'Undo', exact: true })).toBeEnabled();
	});

	test('dropping on the floating assistant card still inserts a via', async ({ page }) => {
		// A large floating overlay - collapsed to a pill by default, sitting
		// bottom-right over the canvas - makes this failure mode far likelier
		// than a small button ever did.
		await page.route('**/api/config', async (route) => {
			const response = await route.fetch();
			await route.fulfill({ json: { ...(await response.json()), assistant_enabled: true } });
		});
		await page.goto('/');
		const { markers, grab } = await planAndGrab(page);

		const pill = page.getByRole('button', { name: 'Ask for a route' });
		await expect(pill).toBeVisible();
		const pillBox = (await pill.boundingBox())!;
		await dragFromTo(page, grab, [pillBox.x + pillBox.width / 2, pillBox.y + pillBox.height / 2]);

		await expect(markers).toHaveCount(3, { timeout: 30_000 });
		await expect(page.getByRole('button', { name: 'Undo', exact: true })).toBeEnabled();
	});
});
