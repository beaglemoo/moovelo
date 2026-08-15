import { expect, test, type Locator, type Page } from '@playwright/test';

const GUIDE_STORAGE_KEY = 'moovelo:planner-guide-dismissed';
const ROUTE_LINE = '__tucB~~s`B?_glW';

function savedRoute(source: 'planned' | 'imported') {
	return {
		id: '11111111-1111-4111-8111-111111111111',
		name: 'PWA layout route',
		preset: 'road',
		costing_options: null,
		source,
		tags: [],
		notes: null,
		is_favourite: false,
		waypoints: [
			{ lat: 52.8, lon: -1.6 },
			{ lat: 52.8, lon: -1.2 }
		],
		legs: [{ geometry: ROUTE_LINE, maneuvers: [] }],
		distance_m: 27_000,
		duration_s: 3600,
		ascent_m: 100,
		descent_m: 100,
		elevation: [],
		surface: null,
		climbs: [],
		ride_time: [],
		updated_at: '2026-08-15T12:00:00Z',
		wahoo: { status: 'none', error: null, route_id: null, pushed_at: null },
		share_token: null
	};
}

async function mockAuthenticatedPlanner(
	page: Page,
	config: Partial<{
		search_enabled: boolean;
		search_index_version: string | null;
		weather_enabled: boolean;
		assistant_enabled: boolean;
	}> = {}
) {
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
				assistant_enabled: false,
				...config
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

async function waitForMap(page: Page): Promise<Locator> {
	const canvas = page.locator('.map canvas').first();
	await expect(canvas).toBeVisible();
	// The canvas exists before MapLibre fires load and installs interactions.
	await page.waitForTimeout(1500);
	return canvas;
}

async function expectNoOverlap(first: Locator, second: Locator, label: string) {
	await expect(first, `${label}: first control should be visible`).toBeVisible();
	await expect(second, `${label}: second control should be visible`).toBeVisible();
	const a = await first.boundingBox();
	const b = await second.boundingBox();
	expect(a, `${label}: first control should have geometry`).not.toBeNull();
	expect(b, `${label}: second control should have geometry`).not.toBeNull();
	const overlaps =
		a!.x < b!.x + b!.width &&
		a!.x + a!.width > b!.x &&
		a!.y < b!.y + b!.height &&
		a!.y + a!.height > b!.y;
	expect(overlaps, label).toBeFalsy();
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

	test('uses a crisp map-paper header in light and dark themes', async ({ page }) => {
		await page.addInitScript(() => localStorage.setItem('moovelo:theme', 'dark'));
		await mockAuthenticatedPlanner(page);
		await page.goto('/');

		const nav = page.locator('nav');
		const menuButton = page.getByRole('button', { name: 'Menu' });
		await expect(nav).toBeVisible();
		await expect(menuButton).toBeVisible();
		const closed = await page.evaluate(() => {
			const navStyle = getComputedStyle(document.querySelector('nav')!);
			const menuStyle = getComputedStyle(document.querySelector('.mobile-menu-toggle')!);
			return {
				navHeight: document.querySelector('nav')!.getBoundingClientRect().height,
				navBackground: navStyle.backgroundColor,
				navColor: navStyle.color,
				navFilter: navStyle.filter,
				navBackdropFilter: navStyle.backdropFilter,
				navBoxShadow: navStyle.boxShadow,
				menuHeight: document.querySelector('.mobile-menu-toggle')!.getBoundingClientRect().height,
				menuBackground: menuStyle.backgroundColor,
				menuColor: menuStyle.color,
				menuBorder: menuStyle.borderColor
			};
		});
		expect(closed).toEqual({
			navHeight: 44,
			navBackground: 'rgb(238, 232, 213)',
			navColor: 'rgb(7, 54, 66)',
			navFilter: 'none',
			navBackdropFilter: 'none',
			navBoxShadow: 'none',
			menuHeight: 44,
			menuBackground: 'rgba(0, 0, 0, 0)',
			menuColor: 'rgb(7, 54, 66)',
			menuBorder: 'rgb(38, 139, 210)'
		});

		await menuButton.focus();
		const focus = await menuButton.evaluate((button) => {
			const style = getComputedStyle(button);
			return { style: style.outlineStyle, width: style.outlineWidth, color: style.outlineColor };
		});
		expect(focus).toEqual({ style: 'solid', width: '3px', color: 'rgb(38, 139, 210)' });

		await page.keyboard.press('Enter');
		await expect(menuButton).toHaveAttribute('aria-expanded', 'true');
		const expanded = await menuButton.evaluate((button) => {
			const style = getComputedStyle(button);
			return { background: style.backgroundColor, color: style.color };
		});
		expect(expanded).toEqual({ background: 'rgb(26, 111, 176)', color: 'rgb(255, 255, 255)' });
		const menuBox = await page.locator('.mobile-menu').boundingBox();
		expect(menuBox).not.toBeNull();
		expect(menuBox!.y).toBe(44);
	});

	test('shows, dismisses and persists the planner guide without startup flash', async ({
		page
	}) => {
		await mockAuthenticatedPlanner(page);
		await page.goto('/');
		const guide = page.getByRole('note', { name: 'Planner guide' });
		await expect(guide).toContainText('Add a start and finish on the map.');
		await guide.getByRole('button', { name: 'Dismiss planner guide' }).tap();
		await expect(guide).toBeHidden();
		expect(await page.evaluate((key) => localStorage.getItem(key), GUIDE_STORAGE_KEY)).toBe('1');
		await page.reload();
		await expect(guide).toHaveCount(0);

		const existing = await page.context().newPage();
		await existing.addInitScript((key) => {
			localStorage.setItem(key, 'true');
			const state = window as typeof window & { plannerGuideWasRendered?: boolean };
			state.plannerGuideWasRendered = false;
			new MutationObserver(() => {
				if (document.querySelector('.planner-guide')) state.plannerGuideWasRendered = true;
			}).observe(document, { childList: true, subtree: true });
		}, GUIDE_STORAGE_KEY);
		await mockAuthenticatedPlanner(existing);
		await existing.goto('/');
		await expect(existing.locator('.planner-guide')).toHaveCount(0);
		expect(
			await existing.evaluate(
				() =>
					(window as typeof window & { plannerGuideWasRendered?: boolean }).plannerGuideWasRendered
			)
		).toBe(false);
		await existing.close();
	});

	test('keeps an in-memory guide dismissal when its storage key is unavailable', async ({
		page
	}) => {
		await page.addInitScript((key) => {
			const nativeGet = Storage.prototype.getItem;
			const nativeSet = Storage.prototype.setItem;
			Storage.prototype.getItem = function (candidate) {
				if (candidate === key) throw new DOMException('Storage blocked', 'SecurityError');
				return nativeGet.call(this, candidate);
			};
			Storage.prototype.setItem = function (candidate, value) {
				if (candidate === key) throw new DOMException('Storage blocked', 'SecurityError');
				return nativeSet.call(this, candidate, value);
			};
		}, GUIDE_STORAGE_KEY);
		await mockAuthenticatedPlanner(page);
		await page.goto('/');
		await page.getByRole('button', { name: 'Dismiss planner guide' }).tap();
		await expect(page.locator('.planner-guide')).toHaveCount(0);

		await page.getByRole('button', { name: 'Menu' }).tap();
		await page.locator('.mobile-menu').getByRole('link', { name: 'Library' }).tap();
		await page.getByRole('button', { name: 'Menu' }).tap();
		await page.locator('.mobile-menu').getByRole('link', { name: 'Planner' }).tap();
		await expect(page.locator('.planner-guide')).toHaveCount(0);
	});

	test('dismisses the guide after the first map waypoint', async ({ page }) => {
		await mockAuthenticatedPlanner(page);
		await page.goto('/');
		const guide = page.locator('.planner-guide');
		await expect(guide).toBeVisible();
		const canvas = await waitForMap(page);
		const box = await canvas.boundingBox();
		expect(box).not.toBeNull();
		await page.touchscreen.tap(box!.x + box!.width * 0.5, box!.y + box!.height * 0.55);
		await expect(page.locator('.maplibregl-marker')).toHaveCount(1);
		await expect(guide).toHaveCount(0);
		expect(await page.evaluate((key) => localStorage.getItem(key), GUIDE_STORAGE_KEY)).toBe('1');
	});

	test('dismisses the guide after adding a searched place', async ({ page }) => {
		await mockAuthenticatedPlanner(page, { search_enabled: true });
		await page.route('**/api/places/search?**', (route) =>
			route.fulfill({
				json: [
					{
						id: 1,
						name: 'Tring',
						place_type: 'town',
						lat: 51.794,
						lon: -0.66,
						distance_m: 1200
					}
				]
			})
		);
		await page.goto('/');
		await page.getByPlaceholder('Search for a place').fill('Tring');
		await page.getByRole('option', { name: /Tring/ }).getByRole('button', { name: /Tring/ }).tap();
		await expect(page.locator('.maplibregl-marker')).toHaveCount(1);
		await expect(page.locator('.planner-guide')).toHaveCount(0);
		expect(await page.evaluate((key) => localStorage.getItem(key), GUIDE_STORAGE_KEY)).toBe('1');
	});

	for (const action of ['Route from here', 'Route to here']) {
		test(`dismisses the guide after the ${action.toLowerCase()} action`, async ({ page }) => {
			await mockAuthenticatedPlanner(page);
			await page.goto('/');
			const canvas = await waitForMap(page);
			const box = await canvas.boundingBox();
			expect(box).not.toBeNull();
			await page.mouse.click(box!.x + box!.width * 0.5, box!.y + box!.height * 0.55, {
				button: 'right'
			});
			await page.getByRole('menuitem', { name: action }).click();
			await expect(page.locator('.maplibregl-marker')).toHaveCount(1);
			await expect(page.locator('.planner-guide')).toHaveCount(0);
			expect(await page.evaluate((key) => localStorage.getItem(key), GUIDE_STORAGE_KEY)).toBe('1');
		});
	}

	test('keeps the guide after a failed search that adds no waypoint', async ({ page }) => {
		await mockAuthenticatedPlanner(page, { search_enabled: true });
		await page.route('**/api/places/search?**', (route) =>
			route.fulfill({ status: 503, json: { detail: 'Index unavailable' } })
		);
		await page.goto('/');
		await page.getByPlaceholder('Search for a place').fill('Tring');
		await expect(page.getByText('Search failed. Try again.')).toBeVisible();
		await expect(page.locator('.planner-guide')).toHaveCount(0);
		await page.getByPlaceholder('Search for a place').press('Escape');
		await expect(page.locator('.planner-guide')).toBeVisible();
		expect(await page.evaluate((key) => localStorage.getItem(key), GUIDE_STORAGE_KEY)).toBeNull();
	});

	test('does not persist dismissal when an imported-route edit is refused', async ({ page }) => {
		await mockAuthenticatedPlanner(page);
		await page.route('**/api/routes/imported', (route) =>
			route.fulfill({ json: savedRoute('imported') })
		);
		await page.goto('/?route=imported');
		const canvas = await waitForMap(page);
		await expect(page.locator('.maplibregl-marker')).toHaveCount(2);
		const box = await canvas.boundingBox();
		expect(box).not.toBeNull();
		await page.mouse.click(box!.x + box!.width * 0.5, box!.y + box!.height * 0.3, {
			button: 'right'
		});
		page.once('dialog', (dialog) => void dialog.dismiss());
		await page.getByRole('menuitem', { name: 'Route from here' }).click();
		await expect(page.locator('.maplibregl-marker')).toHaveCount(2);
		expect(await page.evaluate((key) => localStorage.getItem(key), GUIDE_STORAGE_KEY)).toBeNull();
	});

	test('keeps the guide clear of every top and bottom overlay at phone sizes', async ({ page }) => {
		await mockAuthenticatedPlanner(page, { search_enabled: true, assistant_enabled: true });
		await page.route('**/api/routes/missing', (route) =>
			route.fulfill({ status: 404, json: { detail: 'Not found' } })
		);
		await page.goto('/?route=missing');
		await waitForMap(page);
		await expect(page.locator('.banner.error')).toHaveText('Could not load that route.');

		for (const viewport of [
			{ width: 320, height: 568 },
			{ width: 390, height: 844 },
			{ width: 844, height: 390 }
		]) {
			await page.setViewportSize(viewport);
			const guide = page.locator('.planner-guide');
			await expectNoOverlap(guide, page.locator('.toolbar'), `${viewport.width}: toolbar`);
			await expectNoOverlap(guide, page.locator('.search-bar'), `${viewport.width}: search`);
			await expectNoOverlap(
				guide,
				page.locator('.maplibregl-ctrl-top-right'),
				`${viewport.width}: zoom controls`
			);
			await expectNoOverlap(guide, page.locator('.basemap-switch'), `${viewport.width}: basemap`);
			await expectNoOverlap(guide, page.locator('.assistant-pill'), `${viewport.width}: assistant`);
			await expectNoOverlap(guide, page.locator('.banner.error'), `${viewport.width}: error`);
			const dimensions = await page.evaluate(() => ({
				clientWidth: document.documentElement.clientWidth,
				scrollWidth: document.documentElement.scrollWidth,
				bodyScrollWidth: document.body.scrollWidth
			}));
			expect(dimensions.scrollWidth).toBe(dimensions.clientWidth);
			expect(dimensions.bodyScrollWidth).toBe(dimensions.clientWidth);
		}
	});

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
		expect(box!.y).toBeGreaterThanOrEqual(44);
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

test('desktop navigation and ordinary-page scrolling remain unchanged', async ({ page }) => {
	await page.setViewportSize({ width: 1024, height: 720 });
	await mockAuthenticatedPlanner(page);
	await page.route('**/api/routes/tags', (route) => route.fulfill({ json: [] }));
	await page.route('**/api/routes', (route) => route.fulfill({ json: [] }));
	await page.goto('/');

	const nav = page.locator('nav');
	await expect(page.locator('.desktop-nav')).toBeVisible();
	await expect(page.locator('.mobile-menu-toggle')).toBeHidden();
	const desktopStyle = await nav.evaluate((element) => {
		const style = getComputedStyle(element);
		return { height: element.getBoundingClientRect().height, background: style.backgroundColor };
	});
	expect(desktopStyle).toEqual({ height: 42, background: 'rgb(7, 54, 66)' });

	await page.locator('.desktop-nav').getByRole('link', { name: 'Library' }).click();
	await expect(page).toHaveURL(/\/library$/);
	const mainScroll = await page.locator('main').evaluate((main) => {
		const spacer = document.createElement('div');
		spacer.style.height = '2000px';
		main.append(spacer);
		main.scrollTop = 400;
		return { top: main.scrollTop, overflowY: getComputedStyle(main).overflowY };
	});
	expect(mainScroll).toEqual({ top: 400, overflowY: 'auto' });
	expect(await page.evaluate(() => ({ x: window.scrollX, y: window.scrollY }))).toEqual({
		x: 0,
		y: 0
	});
});
