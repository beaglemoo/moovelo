import { expect, test } from '@playwright/test';

async function mockAuthenticatedPlanner(page: import('@playwright/test').Page) {
	await page.route('**/api/auth/me', (route) =>
		route.fulfill({ json: { email: 'rider@example.com', is_admin: true } })
	);
	await page.route('**/api/config', (route) =>
		route.fulfill({
			json: {
				tile_url_cyclosm: null,
				search_enabled: false,
				search_index_version: null,
				weather_enabled: false,
				assistant_enabled: false
			}
		})
	);
	await page.route('**/api/wahoo/status', (route) =>
		route.fulfill({ json: { configured: false, connected: false, athlete: null } })
	);
	await page.route('**/api/activities/heatmap-available', (route) =>
		route.fulfill({ json: { available: false } })
	);
}

// Runs against a production build served by `vite preview` (see
// playwright.pwa.config.ts). A service worker only exists in a real build, so
// none of this is meaningful against the dev server.

test('the web manifest is valid and lists the icon set', async ({ page }) => {
	const res = await page.request.get('/manifest.webmanifest');
	expect(res.ok()).toBeTruthy();

	const manifest = JSON.parse(await res.text());
	expect(manifest.name).toBe('Moovelo');
	expect(manifest.display).toBe('standalone');
	expect(manifest.start_url).toBe('/');

	const sizes = manifest.icons.map((i: { sizes: string }) => i.sizes);
	expect(sizes).toContain('192x192');
	expect(sizes).toContain('512x512');
	const hasMaskable = manifest.icons.some((i: { purpose?: string }) => i.purpose === 'maskable');
	expect(hasMaskable).toBeTruthy();

	// Every referenced icon actually exists.
	for (const icon of manifest.icons as { src: string }[]) {
		const iconRes = await page.request.get(icon.src);
		expect(iconRes.ok(), `${icon.src} should be served`).toBeTruthy();
	}

	await page.goto('/');
	await expect(page.locator('meta[name="apple-mobile-web-app-status-bar-style"]')).toHaveAttribute(
		'content',
		'black'
	);
});

test('the service worker registers and precaches under a versioned cache', async ({ page }) => {
	await page.goto('/');
	await page.waitForFunction(() => navigator.serviceWorker?.ready !== undefined);
	await page.evaluate(() => navigator.serviceWorker.ready);

	const cacheNames = await page.evaluate(() => caches.keys());
	const versioned = cacheNames.filter((n) => n.startsWith('moovelo-cache-'));
	expect(versioned.length, 'exactly one versioned cache').toBe(1);

	// The precache is non-empty and same-origin only.
	const origin = new URL(page.url()).origin;
	const urls = await page.evaluate(async () => {
		const key = (await caches.keys()).find((n) => n.startsWith('moovelo-cache-'));
		const cache = await caches.open(key!);
		return (await cache.keys()).map((r) => r.url);
	});
	expect(urls.length).toBeGreaterThan(0);
	for (const u of urls) {
		expect(new URL(u).origin, `${u} should be same-origin`).toBe(origin);
	}
});

test('serves the app shell offline after a single online load', async ({ page, context }) => {
	// The reported first-session scenario: one (uncontrolled) online load, then
	// offline before any second navigation. The shell must be precached at
	// install or this reload white-screens.
	await page.goto('/');
	await page.evaluate(() => navigator.serviceWorker.ready);

	await context.setOffline(true);
	try {
		const res = await page.reload();
		expect(res?.ok(), 'offline reload should be served from cache').toBeTruthy();
		await expect(page).toHaveTitle(/Moovelo/);
	} finally {
		await context.setOffline(false);
	}
});

test('the cache never holds /api or cross-origin requests', async ({ page }) => {
	await page.goto('/');
	await page.evaluate(() => navigator.serviceWorker.ready);
	// Give any /api probes and asset loads a beat to settle through the SW.
	await page.reload();
	await page.evaluate(() => navigator.serviceWorker.ready);

	const origin = new URL(page.url()).origin;
	const urls = await page.evaluate(async () => {
		const out: string[] = [];
		for (const key of await caches.keys()) {
			const cache = await caches.open(key);
			for (const req of await cache.keys()) out.push(req.url);
		}
		return out;
	});

	for (const u of urls) {
		expect(new URL(u).pathname.startsWith('/api/'), `${u} must not be cached`).toBeFalsy();
		expect(new URL(u).origin, `${u} cross-origin must not be cached`).toBe(origin);
	}
});

test.describe('mobile PWA', () => {
	test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });

	test('contains scrolling and keeps both control rows touchable', async ({ page }) => {
		await mockAuthenticatedPlanner(page);
		await page.goto('/');

		const nav = page.locator('nav');
		const menuButton = page.getByRole('button', { name: 'Menu' });
		await expect(nav).toBeVisible();
		await expect(menuButton).toBeVisible();
		await expect(page.locator('.desktop-nav')).toBeHidden();
		expect(await page.evaluate(() => navigator.maxTouchPoints)).toBeGreaterThan(0);

		const dimensions = await page.evaluate(() => ({
			clientWidth: document.documentElement.clientWidth,
			scrollWidth: document.documentElement.scrollWidth,
			bodyScrollWidth: document.body.scrollWidth,
			navTop: document.querySelector('nav')?.getBoundingClientRect().top
		}));
		expect(dimensions.scrollWidth).toBe(dimensions.clientWidth);
		expect(dimensions.bodyScrollWidth).toBe(dimensions.clientWidth);
		expect(dimensions.navTop).toBeGreaterThanOrEqual(0);

		await page.evaluate(() => window.scrollTo(300, 300));
		expect(await page.evaluate(() => ({ x: window.scrollX, y: window.scrollY }))).toEqual({
			x: 0,
			y: 0
		});

		await menuButton.tap();
		await expect(menuButton).toHaveAttribute('aria-expanded', 'true');
		const mobileMenu = page.locator('.mobile-menu');
		for (const name of ['Planner', 'Library', 'Activities', 'Settings', 'Admin', 'Log out']) {
			await expect(mobileMenu.getByText(name, { exact: true })).toBeVisible();
		}
		await expect(mobileMenu.getByRole('button', { name: 'Theme: Auto' })).toBeVisible();
		await expect(mobileMenu.getByRole('button', { name: 'Units: km' })).toBeVisible();
		await mobileMenu.getByRole('button', { name: 'Theme: Auto' }).tap();
		await expect(mobileMenu.getByRole('button', { name: 'Theme: Light' })).toBeVisible();
		expect(await page.evaluate(() => localStorage.getItem('moovelo:theme'))).toBe('light');
		await mobileMenu.getByRole('button', { name: 'Units: km' }).tap();
		await expect(mobileMenu.getByRole('button', { name: 'Units: mi' })).toBeVisible();
		expect(await page.evaluate(() => localStorage.getItem('moovelo:units'))).toBe('imperial');
		await mobileMenu.getByRole('link', { name: 'Library' }).tap();
		await expect(page).toHaveURL(/\/library$/);

		// Escape dismisses and restores focus to the control that opened the
		// overlay, instead of dropping keyboard users onto the document body.
		await page.goto('/');
		await menuButton.focus();
		await page.keyboard.press('Enter');
		await page.locator('.mobile-menu').getByRole('link', { name: 'Library' }).focus();
		await page.keyboard.press('Escape');
		await expect(page.locator('.mobile-menu')).toBeHidden();
		await expect(menuButton).toBeFocused();

		const canvas = page.locator('.map canvas').first();
		await expect(canvas).toBeVisible();
		await page.waitForTimeout(1500);
		const canvasBox = await canvas.boundingBox();
		expect(canvasBox).not.toBeNull();

		// The first tap outside dismisses only. It must not fall through to
		// MapLibre and add a waypoint underneath the menu.
		await menuButton.tap();
		await page.touchscreen.tap(
			canvasBox!.x + canvasBox!.width * 0.75,
			canvasBox!.y + canvasBox!.height * 0.75
		);
		await expect(page.locator('.mobile-menu')).toBeHidden();
		await expect(page.getByRole('button', { name: 'Clear' })).toBeDisabled();

		// WebKit can composite MapLibre's canvas separately. Test the rendered
		// hit layer, not only CSS z-index: every toolbar button's centre must
		// resolve back to that button instead of the canvas underneath it.
		const hitTargets = await page.locator('.toolbar button:visible').evaluateAll((buttons) =>
			buttons.map((button) => {
				const rect = button.getBoundingClientRect();
				const hit = document.elementFromPoint(
					rect.left + rect.width / 2,
					rect.top + rect.height / 2
				);
				return { label: button.textContent?.trim(), hitsButton: hit?.closest('button') === button };
			})
		);
		for (const target of hitTargets) {
			expect(target.hitsButton, `${target.label} should own its touch target`).toBeTruthy();
		}

		await page.getByRole('radio', { name: 'Gravel' }).tap();
		await expect(page.getByRole('radio', { name: 'Gravel' })).toHaveAttribute(
			'aria-checked',
			'true'
		);
		await expect(page.getByRole('button', { name: 'Undo' })).toBeEnabled();
		await page.getByRole('button', { name: 'Undo' }).tap();
		await expect(page.getByRole('radio', { name: 'Road' })).toHaveAttribute('aria-checked', 'true');
	});

	test('keeps every menu action reachable in landscape', async ({ page }) => {
		await page.setViewportSize({ width: 844, height: 390 });
		await mockAuthenticatedPlanner(page);
		await page.goto('/');
		await page.getByRole('button', { name: 'Menu' }).tap();

		const menu = page.locator('.mobile-menu');
		const scrollSize = await menu.evaluate((element) => ({
			clientHeight: element.clientHeight,
			scrollHeight: element.scrollHeight
		}));
		expect(scrollSize.scrollHeight).toBeGreaterThan(scrollSize.clientHeight);

		const logout = menu.getByRole('button', { name: 'Log out' });
		await logout.scrollIntoViewIfNeeded();
		const box = await logout.boundingBox();
		expect(box).not.toBeNull();
		expect(box!.height).toBeGreaterThanOrEqual(44);
		expect(box!.y).toBeGreaterThanOrEqual(42);
		expect(box!.y + box!.height).toBeLessThanOrEqual(390);
	});

	test('a failed planner request never exposes WebKit Load failed', async ({ page }) => {
		await mockAuthenticatedPlanner(page);
		await page.route('**/api/route', (route) => route.abort('failed'));
		await page.goto('/');

		const canvas = page.locator('.map canvas').first();
		await expect(canvas).toBeVisible();
		// Canvas visibility precedes MapLibre's load event; interactions are
		// registered from that handler. Clear becoming enabled after the first
		// tap proves that the tap reached the planner.
		await page.waitForTimeout(1500);
		const box = await canvas.boundingBox();
		expect(box).not.toBeNull();
		await page.touchscreen.tap(box!.x + box!.width * 0.35, box!.y + box!.height * 0.55);
		await expect(page.getByRole('button', { name: 'Clear' })).toBeEnabled();
		await page.touchscreen.tap(box!.x + box!.width * 0.65, box!.y + box!.height * 0.65);

		await expect(page.locator('.banner.error')).toHaveText(
			'Cannot reach the server - check it is running.'
		);
		await expect(page.getByText('Load failed', { exact: true })).toHaveCount(0);
	});

	test('logs out from the compact menu', async ({ page }) => {
		let loggedOut = false;
		await mockAuthenticatedPlanner(page);
		// Registered after the shared mock, so this stateful session route wins.
		await page.route('**/api/auth/me', (route) =>
			loggedOut
				? route.fulfill({ status: 401, json: { detail: 'Not authenticated' } })
				: route.fulfill({ json: { email: 'rider@example.com', is_admin: true } })
		);
		await page.route('**/api/auth/logout', (route) => {
			loggedOut = true;
			return route.fulfill({ json: { status: 'ok' } });
		});
		await page.goto('/');
		await page.getByRole('button', { name: 'Menu' }).tap();
		await page.locator('.mobile-menu').getByRole('button', { name: 'Log out' }).tap();

		await expect(page).toHaveURL(/\/login$/);
		await expect(page.locator('nav')).toHaveCount(0);
	});
});
