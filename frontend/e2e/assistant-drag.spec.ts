import { expect, test } from '@playwright/test';

// Dragging the floating assistant card by its header.
//
// The card sits on top of the map, so the gesture that moves it must never
// reach the map underneath - not a pan, not a dropped waypoint - the way an
// earlier drag on this map (the route-line drag-to-reroute) once leaked past
// a floating control because it only ended on the map's own mouseup. This
// one uses Pointer Events with setPointerCapture instead, precisely to avoid
// that failure mode; these specs prove it rather than assume it.

const password = 'e2e-assistant-drag-password-1';
let accounts = 0;

async function signIn(page: import('@playwright/test').Page) {
	const status = await page.request.get('/api/auth/status').then((r) => r.json());
	test.skip(
		!(status.setup_required || (status.signups_enabled && status.password_login)),
		'needs a fresh DB or SIGNUPS_ENABLED=true with password login'
	);
	const email = `e2e-assistant-drag-${Date.now()}-${accounts++}@example.com`;
	const registered = await page.request.post('/api/auth/register', { data: { email, password } });
	expect(registered.ok()).toBeTruthy();
}

async function enableAssistant(page: import('@playwright/test').Page) {
	await page.route('**/api/config', async (route) => {
		const response = await route.fetch();
		await route.fulfill({ json: { ...(await response.json()), assistant_enabled: true } });
	});
}

async function dragHeader(
	page: import('@playwright/test').Page,
	header: import('@playwright/test').Locator,
	dx: number,
	dy: number
) {
	const box = (await header.boundingBox())!;
	const startX = box.x + box.width / 2;
	const startY = box.y + box.height / 2;
	await page.mouse.move(startX, startY);
	await page.mouse.down();
	for (let i = 1; i <= 10; i++) {
		await page.mouse.move(startX + (dx * i) / 10, startY + (dy * i) / 10);
		await page.waitForTimeout(30);
	}
	await page.mouse.up();
}

test('dragging the assistant by its header never pans the map or drops a waypoint', async ({
	page
}) => {
	await signIn(page);
	await enableAssistant(page);
	await page.goto('/');

	const canvas = page.locator('.map canvas').first();
	await expect(canvas).toBeVisible();
	await page.waitForTimeout(2500);

	// An existing waypoint marker is the probe for panning: MapLibre
	// repositions it to track the camera, so if the drag reached the map's
	// own pan handling, the marker would move on screen even though its
	// lat/lon did not change.
	const box = (await canvas.boundingBox())!;
	await page.mouse.click(box.x + box.width / 2 - 60, box.y + box.height / 2 - 40);
	const markers = page.locator('.maplibregl-marker');
	await expect(markers).toHaveCount(1);
	const markerBefore = (await markers.first().boundingBox())!;

	await page.getByRole('button', { name: 'Ask for a route' }).click();
	const header = page.locator('.assistant-head');
	await expect(header).toBeVisible();
	const headerBefore = (await header.boundingBox())!;

	// A long diagonal drag back across the canvas - the exact shape of
	// gesture that used to fall through to the map when it ended over a
	// floating control instead of the canvas itself.
	await dragHeader(page, header, -220, -120);

	// No waypoint appeared, and the one already there has not moved.
	await expect(markers).toHaveCount(1);
	const markerAfter = (await markers.first().boundingBox())!;
	expect(Math.abs(markerAfter.x - markerBefore.x)).toBeLessThan(1);
	expect(Math.abs(markerAfter.y - markerBefore.y)).toBeLessThan(1);

	// The card itself did move - a positive control proving the drag was a
	// real gesture and not a no-op.
	const headerAfter = (await header.boundingBox())!;
	expect(
		Math.hypot(headerAfter.x - headerBefore.x, headerAfter.y - headerBefore.y)
	).toBeGreaterThan(100);

	// And the map still answers ordinary clicks afterwards - the drag left
	// no stray listener holding it in a broken state.
	await page.mouse.click(box.x + box.width / 2 + 60, box.y + box.height / 2 + 40);
	await expect(markers).toHaveCount(2);
});

test('the dragged position persists across reload and re-clamps to a narrower window', async ({
	page
}) => {
	await signIn(page);
	await enableAssistant(page);
	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto('/');

	const canvas = page.locator('.map canvas').first();
	await expect(canvas).toBeVisible();
	await page.waitForTimeout(2500);

	await page.getByRole('button', { name: 'Ask for a route' }).click();
	const header = page.locator('.assistant-head');
	await expect(header).toBeVisible();

	// Park it near the top-right of the wide window - far enough right that
	// its literal pixel position would sit well past the right edge of the
	// narrower window used below, if nothing re-clamped it.
	const mapBox = (await canvas.boundingBox())!;
	const headerBox = (await header.boundingBox())!;
	await dragHeader(
		page,
		header,
		mapBox.x + mapBox.width - 60 - (headerBox.x + headerBox.width / 2),
		mapBox.y + 60 - (headerBox.y + headerBox.height / 2)
	);
	const parked = (await header.boundingBox())!;

	const stored = await page.evaluate(() => localStorage.getItem('moovelo:assistant-box'));
	expect(stored).toBeTruthy();
	const parsed = JSON.parse(stored!) as { collapsed: boolean; left: number; top: number };
	expect(parsed.collapsed).toBe(false);

	await page.reload();
	await expect(canvas).toBeVisible();
	await page.waitForTimeout(1500);
	const afterReload = page.locator('.assistant-head');
	await expect(afterReload).toBeVisible();
	const restored = (await afterReload.boundingBox())!;
	expect(Math.abs(restored.x - parked.x)).toBeLessThan(5);
	expect(Math.abs(restored.y - parked.y)).toBeLessThan(5);

	// Shrink the window - above the 760px mobile breakpoint, so this proves
	// the JS clamp rather than the CSS override that takes over below it.
	// The clamp only guarantees a reachable margin of the header stays
	// visible, not that the whole card fits - a card tucked mostly off an
	// edge on purpose is allowed to stay tucked - so the assertion checks
	// for an overlapping strip, not full containment.
	await page.setViewportSize({ width: 820, height: 800 });
	await page.waitForTimeout(300);
	const afterResize = (await afterReload.boundingBox())!;
	const visibleWidth =
		Math.min(afterResize.x + afterResize.width, 820) - Math.max(afterResize.x, 0);
	const visibleHeight =
		Math.min(afterResize.y + afterResize.height, 800) - Math.max(afterResize.y, 0);
	// The header's own height is only its text line - a few px short of the
	// margin the clamp enforces on the card's top edge - so "reachable"
	// means nearly the whole header strip stays on screen, not a fixed pixel
	// count independent of its size.
	expect(visibleWidth).toBeGreaterThan(20);
	expect(visibleHeight).toBeGreaterThan(parked.height - 4);
});
