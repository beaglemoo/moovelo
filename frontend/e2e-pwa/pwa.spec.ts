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

type ClientPoint = { x: number; y: number };

async function dispatchWebKitTouch(
	target: Locator,
	type: 'touchstart' | 'touchmove' | 'touchend' | 'touchcancel',
	point: ClientPoint
) {
	await target.evaluate(
		(element, { type, point }) => {
			const touch = document.createTouch(window, element, 1, point.x, point.y);
			const changedTouches = document.createTouchList(touch);
			const activeTouches =
				type === 'touchend' || type === 'touchcancel' ? document.createTouchList() : changedTouches;
			element.dispatchEvent(
				new TouchEvent(type, {
					bubbles: true,
					cancelable: true,
					touches: activeTouches,
					targetTouches: activeTouches,
					changedTouches
				})
			);
		},
		{ type, point }
	);
}

async function dispatchWebKitMultiTouch(
	target: Locator,
	type: 'touchstart' | 'touchmove' | 'touchend',
	points: [ClientPoint, ClientPoint]
) {
	await target.evaluate(
		(element, { type, points }) => {
			const changedTouches = document.createTouchList(
				...points.map((point, index) =>
					document.createTouch(window, element, index + 1, point.x, point.y)
				)
			);
			const activeTouches = type === 'touchend' ? document.createTouchList() : changedTouches;
			element.dispatchEvent(
				new TouchEvent(type, {
					bubbles: true,
					cancelable: true,
					touches: activeTouches,
					targetTouches: activeTouches,
					changedTouches
				})
			);
		},
		{ type, points }
	);
}

async function dispatchWebKitPartialTouchEnd(
	target: Locator,
	remaining: ClientPoint,
	lifted: ClientPoint
) {
	await target.evaluate(
		(element, { remaining, lifted }) => {
			const remainingTouch = document.createTouch(window, element, 1, remaining.x, remaining.y);
			const liftedTouch = document.createTouch(window, element, 2, lifted.x, lifted.y);
			const activeTouches = document.createTouchList(remainingTouch);
			element.dispatchEvent(
				new TouchEvent('touchend', {
					bubbles: true,
					cancelable: true,
					touches: activeTouches,
					targetTouches: activeTouches,
					changedTouches: document.createTouchList(liftedTouch)
				})
			);
		},
		{ remaining, lifted }
	);
}

async function webKitTouchGesture(
	page: Page,
	target: Locator,
	start: ClientPoint,
	options: { holdMs?: number; moveTo?: ClientPoint } = {}
) {
	await dispatchWebKitTouch(target, 'touchstart', start);
	if (options.holdMs) await page.waitForTimeout(options.holdMs);
	const end = options.moveTo ?? start;
	if (options.moveTo) await dispatchWebKitTouch(target, 'touchmove', end);
	await dispatchWebKitTouch(target, 'touchend', end);
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
		await mockAuthenticatedPlanner(page);
		await page.goto('/');

		for (const theme of ['light', 'dark']) {
			await page.evaluate((value) => localStorage.setItem('moovelo:theme', value), theme);
			await page.reload();

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
					navWebkitBackdropFilter: navStyle.getPropertyValue('-webkit-backdrop-filter') || 'none',
					navBoxShadow: navStyle.boxShadow,
					navDivider: `${navStyle.borderBottomWidth} ${navStyle.borderBottomStyle} ${navStyle.borderBottomColor}`,
					menuHeight: document.querySelector('.mobile-menu-toggle')!.getBoundingClientRect().height,
					menuBackground: menuStyle.backgroundColor,
					menuColor: menuStyle.color,
					menuBorder: menuStyle.borderColor
				};
			});
			expect(closed, theme).toEqual({
				navHeight: 44,
				navBackground: 'rgb(238, 232, 213)',
				navColor: 'rgb(7, 54, 66)',
				navFilter: 'none',
				navBackdropFilter: 'none',
				navWebkitBackdropFilter: 'none',
				navBoxShadow: 'none',
				navDivider: '1px solid rgb(147, 161, 161)',
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
			expect(focus, theme).toEqual({
				style: 'solid',
				width: '3px',
				color: 'rgb(38, 139, 210)'
			});

			await page.keyboard.press('Enter');
			await expect(menuButton).toHaveAttribute('aria-expanded', 'true');
			const expanded = await menuButton.evaluate((button) => {
				const style = getComputedStyle(button);
				return { background: style.backgroundColor, color: style.color };
			});
			expect(expanded, theme).toEqual({
				background: 'rgb(26, 111, 176)',
				color: 'rgb(255, 255, 255)'
			});
			const menuBox = await page.locator('.mobile-menu').boundingBox();
			expect(menuBox).not.toBeNull();
			expect(menuBox!.y).toBe(44);
		}
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

	test('keeps the guide dismissed for the session when local storage is unavailable', async ({
		page
	}) => {
		await page.addInitScript((key) => {
			const nativeGet = Storage.prototype.getItem;
			const nativeSet = Storage.prototype.setItem;
			Storage.prototype.getItem = function (candidate) {
				if (candidate === key && this === window.localStorage)
					throw new DOMException('Storage blocked', 'SecurityError');
				return nativeGet.call(this, candidate);
			};
			Storage.prototype.setItem = function (candidate, value) {
				if (candidate === key && this === window.localStorage)
					throw new DOMException('Storage blocked', 'SecurityError');
				return nativeSet.call(this, candidate, value);
			};
		}, GUIDE_STORAGE_KEY);
		await mockAuthenticatedPlanner(page);
		await page.goto('/');
		await page.getByRole('button', { name: 'Dismiss planner guide' }).tap();
		await expect(page.locator('.planner-guide')).toHaveCount(0);
		expect(await page.evaluate((key) => sessionStorage.getItem(key), GUIDE_STORAGE_KEY)).toBe('1');
		await page.reload();
		await expect(page.locator('.planner-guide')).toHaveCount(0);

		await page.getByRole('button', { name: 'Menu' }).tap();
		await page.locator('.mobile-menu').getByRole('link', { name: 'Library' }).tap();
		await page.getByRole('button', { name: 'Menu' }).tap();
		await page.locator('.mobile-menu').getByRole('link', { name: 'Planner' }).tap();
		await expect(page.locator('.planner-guide')).toHaveCount(0);
	});

	test('keeps the guide dismissed across navigation when both storage areas are unavailable', async ({
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

	for (const action of ['From', 'To']) {
		test(`dismisses the guide after choosing search result ${action}`, async ({ page }) => {
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
			await page
				.getByRole('option', { name: /Tring/ })
				.getByRole('button', { name: action, exact: true })
				.tap();
			await expect(page.locator('.maplibregl-marker')).toHaveCount(1);
			await expect(page.locator('.planner-guide')).toHaveCount(0);
			expect(await page.evaluate((key) => localStorage.getItem(key), GUIDE_STORAGE_KEY)).toBe('1');
		});
	}

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

	test('does not reopen an abandoned search after its debounce fires', async ({ page }) => {
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
		await page.getByPlaceholder('Search for a place').fill('Tr');
		await page.waitForTimeout(60);
		await page.locator('.brand').tap();
		await expect(page.locator('.planner-guide')).toBeVisible();
		await page.waitForTimeout(350);
		await expect(page.locator('.results')).toHaveCount(0);
		await expect(page.locator('.planner-guide')).toBeVisible();
	});

	for (const dismissal of ['outside click', 'Escape']) {
		test(`does not reopen an in-flight search after ${dismissal}`, async ({ page }) => {
			await mockAuthenticatedPlanner(page, { search_enabled: true });
			let releaseSearch!: () => void;
			const searchReleased = new Promise<void>((resolve) => (releaseSearch = resolve));
			let searchRequested!: () => void;
			const searchRequestSeen = new Promise<void>((resolve) => (searchRequested = resolve));
			await page.route('**/api/places/search?**', async (route) => {
				searchRequested();
				await searchReleased;
				await route.fulfill({
					json: [
						{
							id: 1,
							name: 'Late Tring result',
							place_type: 'town',
							lat: 51.794,
							lon: -0.66,
							distance_m: 1200
						}
					]
				});
			});
			await page.goto('/');
			const search = page.getByPlaceholder('Search for a place');
			await search.fill('Tring');
			await searchRequestSeen;
			if (dismissal === 'Escape') await search.press('Escape');
			else await page.locator('.brand').tap();
			releaseSearch();

			await page.waitForTimeout(100);
			await expect(page.getByText('Late Tring result', { exact: true })).toHaveCount(0);
			await expect(page.locator('.results')).toHaveCount(0);
			await expect(page.locator('.planner-guide')).toBeVisible();
		});
	}

	test('does not show an older search response during the next query debounce', async ({
		page
	}) => {
		await mockAuthenticatedPlanner(page, { search_enabled: true });
		let releaseOld!: () => void;
		const oldReleased = new Promise<void>((resolve) => (releaseOld = resolve));
		let oldRequested!: () => void;
		const oldRequestSeen = new Promise<void>((resolve) => (oldRequested = resolve));
		await page.route('**/api/places/search?**', async (route) => {
			const term = new URL(route.request().url()).searchParams.get('q');
			if (term === 'Tr') {
				oldRequested();
				await oldReleased;
				await route.fulfill({
					json: [
						{
							id: 1,
							name: 'Old Tr result',
							place_type: 'town',
							lat: 51.794,
							lon: -0.66,
							distance_m: 1
						}
					]
				});
				return;
			}
			await route.fulfill({
				json: [
					{
						id: 2,
						name: 'Current Tring result',
						place_type: 'town',
						lat: 51.795,
						lon: -0.65,
						distance_m: 2
					}
				]
			});
		});
		await page.goto('/');
		const search = page.getByPlaceholder('Search for a place');
		await search.fill('Tr');
		await oldRequestSeen;
		await search.fill('Tring');
		releaseOld();

		// The old response lands inside the replacement query's debounce
		// window. It must remain cancelled instead of reopening stale actions
		// for text that is no longer in the input.
		await page.waitForTimeout(100);
		await expect(page.getByText('Old Tr result', { exact: true })).toHaveCount(0);
		await expect(page.locator('.results')).toHaveCount(0);
		await expect(page.locator('.planner-guide')).toBeVisible();

		await expect(page.getByText('Current Tring result', { exact: true })).toBeVisible();
		await expect(page.getByText('Old Tr result', { exact: true })).toHaveCount(0);
	});

	test('does not select a rendered result after the query changes', async ({ page }) => {
		await mockAuthenticatedPlanner(page, { search_enabled: true });
		await page.route('**/api/places/search?**', async (route) => {
			const term = new URL(route.request().url()).searchParams.get('q');
			await route.fulfill({
				json: [
					{
						id: term === 'Tr' ? 1 : 2,
						name: term === 'Tr' ? 'Rendered Tr result' : 'Current Tring result',
						place_type: 'town',
						lat: term === 'Tr' ? 51.794 : 51.795,
						lon: term === 'Tr' ? -0.66 : -0.65,
						distance_m: term === 'Tr' ? 1 : 2
					}
				]
			});
		});
		await page.goto('/');
		const search = page.getByPlaceholder('Search for a place');
		await search.fill('Tr');
		await expect(page.getByText('Rendered Tr result', { exact: true })).toBeVisible();

		await search.fill('Tring');
		await expect(page.locator('.results')).toHaveCount(0);
		await search.press('Enter');
		await expect(page.locator('.maplibregl-marker')).toHaveCount(0);
		await expect(page.locator('.planner-guide')).toBeVisible();
		expect(await page.evaluate((key) => localStorage.getItem(key), GUIDE_STORAGE_KEY)).toBeNull();

		await expect(page.getByText('Current Tring result', { exact: true })).toBeVisible();
	});

	test('never flashes the first-run guide while a saved route is loading', async ({ page }) => {
		await page.addInitScript(() => {
			const state = window as typeof window & { plannerGuideWasRendered?: boolean };
			state.plannerGuideWasRendered = false;
			new MutationObserver(() => {
				if (document.querySelector('.planner-guide')) state.plannerGuideWasRendered = true;
			}).observe(document, { childList: true, subtree: true });
		});
		await mockAuthenticatedPlanner(page);
		let releaseRoute!: () => void;
		const routeReleased = new Promise<void>((resolve) => (releaseRoute = resolve));
		let routeRequested!: () => void;
		const requestSeen = new Promise<void>((resolve) => (routeRequested = resolve));
		await page.route('**/api/routes/planned', async (route) => {
			routeRequested();
			await routeReleased;
			await route.fulfill({ json: savedRoute('planned') });
		});
		await page.goto('/?route=planned');
		await requestSeen;
		await page.waitForTimeout(150);
		await expect(page.locator('.planner-guide')).toHaveCount(0);
		releaseRoute();
		await expect(page.locator('.maplibregl-marker')).toHaveCount(2);
		await expect(page.locator('.planner-guide')).toHaveCount(0);
		expect(
			await page.evaluate(
				() =>
					(window as typeof window & { plannerGuideWasRendered?: boolean }).plannerGuideWasRendered
			)
		).toBe(false);
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

	test('stacks the guide below avoid chips when a loaded route loses an endpoint', async ({
		page,
		browserName
	}) => {
		await mockAuthenticatedPlanner(page, { search_enabled: true, assistant_enabled: true });
		await page.route('**/api/routes/planned', (route) =>
			route.fulfill({ json: savedRoute('planned') })
		);
		await page.route('**/api/route', (route) => route.fulfill({ json: savedRoute('planned') }));
		await page.goto('/?route=planned');
		const canvas = await waitForMap(page);
		await expect(page.locator('.maplibregl-marker')).toHaveCount(2);
		await expect(page.locator('.planner-guide')).toHaveCount(0);

		for (const index of [0, 1, 2]) {
			const box = await canvas.boundingBox();
			expect(box).not.toBeNull();
			const point = { x: box!.x + box!.width * 0.5, y: box!.y + box!.height * 0.5 };
			if (browserName === 'webkit') {
				await webKitTouchGesture(page, canvas, point, { holdMs: 650 });
				await expect(page.locator('.maplibregl-marker')).toHaveCount(2);
			} else {
				await page.mouse.click(point.x, point.y, { button: 'right' });
			}
			await page.getByRole('menuitem', { name: 'Avoid this road' }).click();
			await expect(page.locator('.avoid-chip')).toHaveCount(index + 1);
			await expect(page.locator('.banner')).toHaveCount(0);
		}

		await page.getByRole('button', { name: 'Remove waypoint 1' }).click();
		await expect(page.locator('.maplibregl-marker')).toHaveCount(1);
		await expect(page.locator('.planner-guide')).toBeVisible();
		expect(await page.evaluate((key) => localStorage.getItem(key), GUIDE_STORAGE_KEY)).toBeNull();

		for (const viewport of [
			{ width: 320, height: 568 },
			{ width: 390, height: 844 },
			{ width: 844, height: 390 }
		]) {
			await page.setViewportSize(viewport);
			const guide = page.locator('.planner-guide');
			await expectNoOverlap(guide, page.locator('.avoids'), `${viewport.width}: avoid chips`);
			await expectNoOverlap(guide, page.locator('.toolbar'), `${viewport.width}: toolbar`);
			await expectNoOverlap(guide, page.locator('.search-bar'), `${viewport.width}: search`);
			await expectNoOverlap(
				guide,
				page.locator('.maplibregl-ctrl-top-right'),
				`${viewport.width}: zoom controls`
			);
			await expectNoOverlap(guide, page.locator('.basemap-switch'), `${viewport.width}: basemap`);
			await expectNoOverlap(guide, page.locator('.assistant-pill'), `${viewport.width}: assistant`);
			const dimensions = await page.evaluate(() => ({
				clientWidth: document.documentElement.clientWidth,
				scrollWidth: document.documentElement.scrollWidth,
				bodyScrollWidth: document.body.scrollWidth
			}));
			expect(dimensions.scrollWidth).toBe(dimensions.clientWidth);
			expect(dimensions.bodyScrollWidth).toBe(dimensions.clientWidth);
		}
	});

	test('requires deliberate movement before a route touch inserts a via', async ({
		page,
		browserName
	}) => {
		test.skip(browserName !== 'webkit', 'WebKit exercises the iPhone touch gesture');
		await mockAuthenticatedPlanner(page);
		await page.route('**/api/routes/planned', (route) =>
			route.fulfill({ json: savedRoute('planned') })
		);
		await page.goto('/?route=planned');
		const canvas = await waitForMap(page);
		await expect(page.locator('.maplibregl-marker')).toHaveCount(2);
		const box = await canvas.boundingBox();
		expect(box).not.toBeNull();
		const start = { x: box!.x + box!.width * 0.5, y: box!.y + box!.height * 0.5 };

		await webKitTouchGesture(page, canvas, start, { holdMs: 100 });
		await expect(page.locator('.maplibregl-marker')).toHaveCount(2);
		await webKitTouchGesture(page, canvas, start, {
			moveTo: { x: start.x + 1, y: start.y + 1 }
		});
		await expect(page.locator('.maplibregl-marker')).toHaveCount(2);
		await webKitTouchGesture(page, canvas, start, {
			moveTo: { x: start.x + 35, y: start.y }
		});
		await expect(page.locator('.maplibregl-marker')).toHaveCount(3);
	});

	test('keeps the waypoint menu on a marker long press', async ({ page, browserName }) => {
		test.skip(browserName !== 'webkit', 'WebKit exercises the iPhone touch gesture');
		await mockAuthenticatedPlanner(page);
		await page.route('**/api/routes/planned', (route) =>
			route.fulfill({ json: savedRoute('planned') })
		);
		await page.goto('/?route=planned');
		await waitForMap(page);
		const marker = page.locator('.maplibregl-marker').first();
		const markerBox = await marker.boundingBox();
		expect(markerBox).not.toBeNull();
		await webKitTouchGesture(
			page,
			marker,
			{
				x: markerBox!.x + markerBox!.width * 0.5,
				y: markerBox!.y + markerBox!.height * 0.5
			},
			{ holdMs: 650 }
		);
		await expect(page.getByRole('menuitem', { name: 'Remove waypoint' })).toBeVisible();
		await expect(page.getByRole('menuitem', { name: 'Route from here' })).toHaveCount(0);
	});

	test('allows marker jitter but cancels a deliberate move before long press', async ({
		page,
		browserName
	}) => {
		test.skip(browserName !== 'webkit', 'WebKit exercises the iPhone touch gesture');
		await mockAuthenticatedPlanner(page);
		await page.route('**/api/routes/planned', (route) =>
			route.fulfill({ json: savedRoute('planned') })
		);
		await page.goto('/?route=planned');
		await waitForMap(page);
		const marker = page.locator('.maplibregl-marker').first();
		const markerBox = await marker.boundingBox();
		expect(markerBox).not.toBeNull();
		const start = {
			x: markerBox!.x + markerBox!.width * 0.5,
			y: markerBox!.y + markerBox!.height * 0.5
		};

		await dispatchWebKitTouch(marker, 'touchstart', start);
		await dispatchWebKitTouch(marker, 'touchmove', { x: start.x + 1, y: start.y + 1 });
		await page.waitForTimeout(650);
		await dispatchWebKitTouch(marker, 'touchend', { x: start.x + 1, y: start.y + 1 });
		await expect(page.getByRole('menuitem', { name: 'Remove waypoint' })).toBeVisible();
		await page.keyboard.press('Escape');

		await dispatchWebKitTouch(marker, 'touchstart', start);
		await dispatchWebKitTouch(marker, 'touchmove', { x: start.x + 12, y: start.y });
		await page.waitForTimeout(650);
		await dispatchWebKitTouch(marker, 'touchend', { x: start.x + 12, y: start.y });
		await expect(page.locator('.context-menu')).toHaveCount(0);
	});

	test('keeps waypoint touch menus inside the map at edge sizes', async ({ page, browserName }) => {
		test.skip(browserName !== 'webkit', 'WebKit exercises the iPhone touch gesture');
		await mockAuthenticatedPlanner(page, { search_enabled: true });
		const longPlaceName =
			'Barrow upon Soar Railway Station and Riverside Cycle Route Meeting Point';
		await page.route('**/api/places/reverse?**', async (route) => {
			await new Promise((resolve) => setTimeout(resolve, 150));
			await route.fulfill({
				json: {
					id: 42,
					name: longPlaceName,
					place_type: 'station',
					lat: 52.8,
					lon: -1.6,
					distance_m: 10
				}
			});
		});
		await page.route('**/api/routes/planned', (route) =>
			route.fulfill({ json: savedRoute('planned') })
		);
		await page.goto('/?route=planned');
		await waitForMap(page);

		for (const viewport of [
			{ width: 320, height: 568 },
			{ width: 390, height: 844 },
			{ width: 844, height: 390 }
		]) {
			await page.setViewportSize(viewport);
			const marker = page.locator('.maplibregl-marker').last();
			const markerBox = await marker.boundingBox();
			expect(markerBox).not.toBeNull();
			await webKitTouchGesture(
				page,
				marker,
				{
					x: markerBox!.x + markerBox!.width * 0.5,
					y: markerBox!.y + markerBox!.height * 0.5
				},
				{ holdMs: 650 }
			);
			await expect(page.locator('.context-menu .menu-place')).toHaveText(longPlaceName);
			const mapBox = await page.locator('.map-area').boundingBox();
			const menu = page.locator('.context-menu');
			const menuBox = await menu.boundingBox();
			const remove = menu.getByRole('menuitem', { name: 'Remove waypoint' });
			const removeBox = await remove.boundingBox();
			expect(mapBox).not.toBeNull();
			expect(menuBox).not.toBeNull();
			expect(removeBox).not.toBeNull();
			expect(menuBox!.x).toBeGreaterThanOrEqual(mapBox!.x);
			expect(menuBox!.y).toBeGreaterThanOrEqual(mapBox!.y);
			expect(menuBox!.x + menuBox!.width).toBeLessThanOrEqual(mapBox!.x + mapBox!.width);
			expect(menuBox!.y + menuBox!.height).toBeLessThanOrEqual(mapBox!.y + mapBox!.height);
			expect(removeBox!.height).toBeGreaterThanOrEqual(44);
			const removeOwnsCentre = await remove.evaluate((element) => {
				const rect = element.getBoundingClientRect();
				return (
					document
						.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
						?.closest('button') === element
				);
			});
			expect(removeOwnsCentre, `${viewport.width}: edge menu target`).toBe(true);
			await page.keyboard.press('Escape');
			await expect(menu).toHaveCount(0);
		}
	});

	test('keeps every full route-menu action reachable in landscape', async ({
		page,
		browserName
	}) => {
		test.skip(browserName !== 'webkit', 'WebKit exercises the iPhone touch gesture');
		await page.setViewportSize({ width: 844, height: 390 });
		await mockAuthenticatedPlanner(page);
		await page.route('**/api/routes/planned', (route) =>
			route.fulfill({ json: savedRoute('planned') })
		);
		await page.goto('/?route=planned');
		const canvas = await waitForMap(page);
		const canvasBox = await canvas.boundingBox();
		expect(canvasBox).not.toBeNull();
		await webKitTouchGesture(
			page,
			canvas,
			{
				x: canvasBox!.x + canvasBox!.width * 0.5,
				y: canvasBox!.y + canvasBox!.height * 0.5
			},
			{ holdMs: 650 }
		);

		const menu = page.locator('.context-menu');
		await expect(menu).toBeVisible();
		const mapBox = await page.locator('.map-area').boundingBox();
		const menuBox = await menu.boundingBox();
		expect(mapBox).not.toBeNull();
		expect(menuBox).not.toBeNull();
		expect(menuBox!.x).toBeGreaterThanOrEqual(mapBox!.x);
		expect(menuBox!.y).toBeGreaterThanOrEqual(mapBox!.y);
		expect(menuBox!.x + menuBox!.width).toBeLessThanOrEqual(mapBox!.x + mapBox!.width);
		expect(menuBox!.y + menuBox!.height).toBeLessThanOrEqual(mapBox!.y + mapBox!.height);
		const scrolling = await menu.evaluate((element) => ({
			overflowY: getComputedStyle(element).overflowY,
			clientHeight: element.clientHeight,
			scrollHeight: element.scrollHeight
		}));
		expect(scrolling.overflowY).toBe('auto');
		expect(scrolling.scrollHeight).toBeGreaterThan(scrolling.clientHeight);

		for (const name of [
			'Route from here',
			'Add waypoint',
			'Route to here',
			'Isochrone from here',
			'Loop from here',
			'Avoid this road',
			'Clear route'
		]) {
			const action = menu.getByRole('menuitem', { name });
			await action.scrollIntoViewIfNeeded();
			await expect(action, `${name} should be reachable through the menu scroll`).toBeVisible();
			const actionBox = await action.boundingBox();
			expect(actionBox).not.toBeNull();
			expect(actionBox!.height, `${name} touch target`).toBeGreaterThanOrEqual(44);
			const actionOwnsCentre = await action.evaluate((element) => {
				const rect = element.getBoundingClientRect();
				return (
					document
						.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
						?.closest('button') === element
				);
			});
			expect(actionOwnsCentre, `${name} should own its visible centre`).toBe(true);
		}
	});

	test('keeps touch dragging available on waypoint markers', async ({ page, browserName }) => {
		test.skip(browserName !== 'webkit', 'WebKit exercises the iPhone touch gesture');
		await mockAuthenticatedPlanner(page);
		await page.route('**/api/routes/planned', (route) =>
			route.fulfill({ json: savedRoute('planned') })
		);
		await page.goto('/?route=planned');
		await waitForMap(page);
		const marker = page.locator('.maplibregl-marker').first();
		const before = await marker.boundingBox();
		expect(before).not.toBeNull();
		const start = {
			x: before!.x + before!.width * 0.5,
			y: before!.y + before!.height * 0.5
		};
		await webKitTouchGesture(page, marker, start, {
			moveTo: { x: start.x + 40, y: start.y + 20 }
		});
		await expect
			.poll(async () => {
				const after = await marker.boundingBox();
				return after ? Math.hypot(after.x - before!.x, after.y - before!.y) : 0;
			})
			.toBeGreaterThan(10);
	});

	test('cancels an active route drag when a pinch begins', async ({ page, browserName }) => {
		test.skip(browserName !== 'webkit', 'WebKit exercises the iPhone touch gesture');
		await mockAuthenticatedPlanner(page);
		await page.route('**/api/routes/planned', (route) =>
			route.fulfill({ json: savedRoute('planned') })
		);
		await page.goto('/?route=planned');
		const canvas = await waitForMap(page);
		const box = await canvas.boundingBox();
		expect(box).not.toBeNull();
		const start = { x: box!.x + box!.width * 0.5, y: box!.y + box!.height * 0.5 };
		const moved = { x: start.x + 30, y: start.y };
		await dispatchWebKitTouch(canvas, 'touchstart', start);
		await dispatchWebKitTouch(canvas, 'touchmove', moved);
		await dispatchWebKitMultiTouch(canvas, 'touchmove', [
			moved,
			{ x: moved.x + 20, y: moved.y + 20 }
		]);
		await dispatchWebKitMultiTouch(canvas, 'touchend', [
			moved,
			{ x: moved.x + 20, y: moved.y + 20 }
		]);
		await expect(page.locator('.maplibregl-marker')).toHaveCount(2);
	});

	test('cancels a route drag when a second finger taps without moving', async ({
		page,
		browserName
	}) => {
		test.skip(browserName !== 'webkit', 'WebKit exercises the iPhone touch gesture');
		await mockAuthenticatedPlanner(page);
		await page.route('**/api/routes/planned', (route) =>
			route.fulfill({ json: savedRoute('planned') })
		);
		await page.goto('/?route=planned');
		const canvas = await waitForMap(page);
		const box = await canvas.boundingBox();
		expect(box).not.toBeNull();
		const start = { x: box!.x + box!.width * 0.5, y: box!.y + box!.height * 0.5 };
		const moved = { x: start.x + 30, y: start.y };
		const second = { x: moved.x + 20, y: moved.y + 20 };
		await dispatchWebKitTouch(canvas, 'touchstart', start);
		await dispatchWebKitTouch(canvas, 'touchmove', moved);
		await dispatchWebKitMultiTouch(canvas, 'touchstart', [moved, second]);
		await dispatchWebKitPartialTouchEnd(canvas, moved, second);
		await dispatchWebKitTouch(canvas, 'touchend', moved);
		await expect(page.locator('.maplibregl-marker')).toHaveCount(2);
	});

	test('cancels a route long press when a second finger rests on an overlay', async ({
		page,
		browserName
	}) => {
		test.skip(browserName !== 'webkit', 'WebKit exercises the iPhone touch gesture');
		await mockAuthenticatedPlanner(page);
		await page.route('**/api/routes/planned', (route) =>
			route.fulfill({ json: savedRoute('planned') })
		);
		await page.goto('/?route=planned');
		const canvas = await waitForMap(page);
		const canvasBox = await canvas.boundingBox();
		const zoomIn = page.locator('.maplibregl-ctrl-zoom-in');
		const zoomBox = await zoomIn.boundingBox();
		expect(canvasBox).not.toBeNull();
		expect(zoomBox).not.toBeNull();
		const first = {
			x: canvasBox!.x + canvasBox!.width * 0.5,
			y: canvasBox!.y + canvasBox!.height * 0.5
		};
		const second = {
			x: zoomBox!.x + zoomBox!.width * 0.5,
			y: zoomBox!.y + zoomBox!.height * 0.5
		};

		await dispatchWebKitTouch(canvas, 'touchstart', first);
		await dispatchWebKitMultiTouch(zoomIn, 'touchstart', [first, second]);
		await page.waitForTimeout(650);
		await dispatchWebKitPartialTouchEnd(zoomIn, first, second);
		await dispatchWebKitTouch(canvas, 'touchend', first);
		await expect(page.locator('.context-menu')).toHaveCount(0);
		await expect(page.locator('.maplibregl-marker')).toHaveCount(2);
	});

	test('cancels a marker long press when a second finger joins', async ({ page, browserName }) => {
		test.skip(browserName !== 'webkit', 'WebKit exercises the iPhone touch gesture');
		await mockAuthenticatedPlanner(page);
		await page.route('**/api/routes/planned', (route) =>
			route.fulfill({ json: savedRoute('planned') })
		);
		await page.goto('/?route=planned');
		const canvas = await waitForMap(page);
		const marker = page.locator('.maplibregl-marker').first();
		const markerBox = await marker.boundingBox();
		expect(markerBox).not.toBeNull();
		const first = {
			x: markerBox!.x + markerBox!.width * 0.5,
			y: markerBox!.y + markerBox!.height * 0.5
		};
		const second = { x: first.x + 30, y: first.y + 30 };
		await dispatchWebKitTouch(marker, 'touchstart', first);
		await dispatchWebKitMultiTouch(canvas, 'touchstart', [first, second]);
		await page.waitForTimeout(650);
		await dispatchWebKitPartialTouchEnd(canvas, first, second);
		await dispatchWebKitTouch(marker, 'touchend', first);
		await expect(page.locator('.context-menu')).toHaveCount(0);
	});

	test('cancels route drag state after touchcancel', async ({ page, browserName }) => {
		test.skip(browserName !== 'webkit', 'WebKit exercises the iPhone touch gesture');
		await mockAuthenticatedPlanner(page);
		await page.route('**/api/routes/planned', (route) =>
			route.fulfill({ json: savedRoute('planned') })
		);
		await page.goto('/?route=planned');
		const canvas = await waitForMap(page);
		const box = await canvas.boundingBox();
		expect(box).not.toBeNull();
		const start = { x: box!.x + box!.width * 0.5, y: box!.y + box!.height * 0.5 };
		const moved = { x: start.x + 30, y: start.y };
		await dispatchWebKitTouch(canvas, 'touchstart', start);
		await dispatchWebKitTouch(canvas, 'touchmove', moved);
		await dispatchWebKitTouch(canvas, 'touchcancel', moved);
		// A later gesture's touchend must not complete the cancelled drag. Use
		// synthetic touch events here so this probe does not also create the
		// normal map click that intentionally adds a waypoint.
		await webKitTouchGesture(page, canvas, {
			x: box!.x + box!.width * 0.2,
			y: box!.y + box!.height * 0.2
		});
		await expect(page.locator('.maplibregl-marker')).toHaveCount(2);
	});

	test('closes a route long-press menu on the next off-origin tap', async ({
		page,
		browserName
	}) => {
		test.skip(browserName !== 'webkit', 'WebKit exercises the iPhone touch gesture');
		await mockAuthenticatedPlanner(page);
		await page.route('**/api/routes/planned', (route) =>
			route.fulfill({ json: savedRoute('planned') })
		);
		await page.goto('/?route=planned');
		const canvas = await waitForMap(page);
		const box = await canvas.boundingBox();
		expect(box).not.toBeNull();
		const routePoint = { x: box!.x + box!.width * 0.5, y: box!.y + box!.height * 0.5 };
		await webKitTouchGesture(page, canvas, routePoint, { holdMs: 650 });
		await expect(page.getByRole('menuitem', { name: 'Avoid this road' })).toBeVisible();
		// WebKit may synthesize a click where the long-press began. That click
		// belongs to the same gesture and must not immediately close its menu.
		await page.mouse.click(routePoint.x, routePoint.y);
		await expect(page.getByRole('menuitem', { name: 'Avoid this road' })).toBeVisible();
		await page.touchscreen.tap(box!.x + box!.width * 0.2, box!.y + box!.height * 0.2);
		await expect(page.locator('.context-menu')).toHaveCount(0);
		await expect(page.locator('.maplibregl-marker')).toHaveCount(2);
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

	test('keeps the collapsed assistant compact and edge-anchored on mobile', async ({ page }) => {
		await page.addInitScript(() =>
			localStorage.setItem(
				'moovelo:assistant-box',
				JSON.stringify({ collapsed: true, left: 1400, top: 100 })
			)
		);
		await mockAuthenticatedPlanner(page, { search_enabled: true, assistant_enabled: true });
		await page.route('**/api/routes/planned', (route) =>
			route.fulfill({ json: savedRoute('planned') })
		);
		await page.goto('/?route=planned');
		await expect(page.locator('.maplibregl-marker')).toHaveCount(2);

		for (const viewport of [
			{ width: 320, height: 568 },
			{ width: 390, height: 844 },
			{ width: 844, height: 390 }
		]) {
			await page.setViewportSize(viewport);
			const mapArea = page.locator('.map-area');
			const assistant = page.locator('.assistant.collapsed');
			const button = assistant.getByRole('button', { name: 'Ask for a route' });
			await expect(button.locator('.assistant-pill-short')).toBeVisible();
			await expect(button.locator('.assistant-pill-long')).toBeHidden();
			expect(await button.evaluate((element) => (element as HTMLElement).innerText.trim())).toBe(
				'Ask'
			);
			const mapBox = await mapArea.boundingBox();
			const assistantBox = await assistant.boundingBox();
			expect(mapBox).not.toBeNull();
			expect(assistantBox).not.toBeNull();
			expect(assistantBox!.width).toBeLessThanOrEqual(72);
			expect(assistantBox!.height).toBeGreaterThanOrEqual(44);
			expect(mapBox!.x + mapBox!.width - (assistantBox!.x + assistantBox!.width)).toBe(10);
			await expectNoOverlap(
				assistant,
				page.locator('.basemap-switch'),
				`${viewport.width}: basemap`
			);
			const pillOwnsCentre = await button.evaluate((element) => {
				const rect = element.getBoundingClientRect();
				return (
					document
						.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
						?.closest('button') === element
				);
			});
			expect(pillOwnsCentre, `${viewport.width}: Ask target should own its centre`).toBe(true);

			await page.touchscreen.tap(
				assistantBox!.x + assistantBox!.width / 2,
				assistantBox!.y + assistantBox!.height / 2
			);
			const expanded = page.locator('.assistant:not(.collapsed)');
			const close = expanded.getByRole('button', { name: 'Collapse the assistant' });
			await expect(expanded).toBeVisible();
			await expect(
				expanded.getByRole('textbox', { name: 'Ask the route assistant' })
			).toBeVisible();
			const expandedBox = await expanded.boundingBox();
			const closeBox = await close.boundingBox();
			expect(expandedBox).not.toBeNull();
			expect(closeBox).not.toBeNull();
			expect(expandedBox!.x).toBeGreaterThanOrEqual(mapBox!.x);
			expect(expandedBox!.x + expandedBox!.width).toBeLessThanOrEqual(mapBox!.x + mapBox!.width);
			expect(closeBox!.width).toBeGreaterThanOrEqual(44);
			expect(closeBox!.height).toBeGreaterThanOrEqual(44);
			const closeOwnsCentre = await close.evaluate((element) => {
				const rect = element.getBoundingClientRect();
				return (
					document
						.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
						?.closest('button') === element
				);
			});
			expect(closeOwnsCentre, `${viewport.width}: collapse target should sit above toolbar`).toBe(
				true
			);
			const dimensions = await page.evaluate(() => ({
				clientWidth: document.documentElement.clientWidth,
				scrollWidth: document.documentElement.scrollWidth,
				bodyScrollWidth: document.body.scrollWidth
			}));
			expect(dimensions.scrollWidth).toBe(dimensions.clientWidth);
			expect(dimensions.bodyScrollWidth).toBe(dimensions.clientWidth);
			await close.tap();
			await expect(page.locator('.assistant.collapsed')).toBeVisible();
		}
	});

	test('keeps the expanded assistant onscreen after a desktop drag', async ({ page }) => {
		await page.setViewportSize({ width: 844, height: 390 });
		await page.addInitScript(() =>
			localStorage.setItem(
				'moovelo:assistant-box',
				JSON.stringify({ collapsed: true, left: 1400, top: 100 })
			)
		);
		await mockAuthenticatedPlanner(page, { assistant_enabled: true });
		await page.route('**/api/routes/planned', (route) =>
			route.fulfill({ json: savedRoute('planned') })
		);
		await page.goto('/?route=planned');
		await expect(page.locator('.maplibregl-marker')).toHaveCount(2);
		await page.getByRole('button', { name: 'Ask for a route' }).tap();

		const mapBox = await page.locator('.map-area').boundingBox();
		const assistantBox = await page.locator('.assistant:not(.collapsed)').boundingBox();
		expect(mapBox).not.toBeNull();
		expect(assistantBox).not.toBeNull();
		expect(assistantBox!.x).toBeGreaterThanOrEqual(mapBox!.x);
		expect(assistantBox!.x + assistantBox!.width).toBeLessThanOrEqual(mapBox!.x + mapBox!.width);
		const geolocate = page.locator('.maplibregl-ctrl-geolocate');
		await expectNoOverlap(
			page.locator('.assistant:not(.collapsed)'),
			geolocate,
			'844: expanded assistant vs geolocate'
		);
		const geolocateOwnsCentre = await geolocate.evaluate((element) => {
			const rect = element.getBoundingClientRect();
			return (
				document
					.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
					?.closest('button') === element
			);
		});
		expect(geolocateOwnsCentre).toBe(true);
	});

	test('contains scrolling and keeps both control rows touchable', async ({ page }) => {
		await mockAuthenticatedPlanner(page);
		await page.goto('/');

		const nav = page.locator('nav');
		const menuButton = page.getByRole('button', { name: 'Menu' });
		await expect(nav).toBeVisible();
		await expect(menuButton).toBeVisible();
		await expect(page.locator('.desktop-nav')).toBeHidden();
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

test('reclamps the expanded assistant at the desktop breakpoint', async ({ page }) => {
	await page.setViewportSize({ width: 901, height: 720 });
	await page.addInitScript(() =>
		localStorage.setItem(
			'moovelo:assistant-box',
			JSON.stringify({ collapsed: true, left: 1400, top: 100 })
		)
	);
	await mockAuthenticatedPlanner(page, { assistant_enabled: true });
	await page.route('**/api/routes/planned', (route) =>
		route.fulfill({ json: savedRoute('planned') })
	);
	await page.goto('/?route=planned');
	await expect(page.locator('.maplibregl-marker')).toHaveCount(2);
	await page.getByRole('button', { name: 'Ask for a route' }).click();

	const mapArea = page.locator('.map-area');
	const expanded = page.locator('.assistant:not(.collapsed)');
	const close = expanded.getByRole('button', { name: 'Collapse the assistant' });
	await expect(expanded.getByRole('textbox', { name: 'Ask the route assistant' })).toBeVisible();
	await expect.poll(async () => (await expanded.boundingBox())?.x ?? -1).toBeGreaterThanOrEqual(0);
	await expect
		.poll(async () => {
			const mapBox = await mapArea.boundingBox();
			const assistantBox = await expanded.boundingBox();
			if (!mapBox || !assistantBox) return Number.POSITIVE_INFINITY;
			return assistantBox.x + assistantBox.width - (mapBox.x + mapBox.width);
		})
		.toBeLessThanOrEqual(0);
	await expect(close).toBeVisible();
	const geolocate = page.locator('.maplibregl-ctrl-geolocate');
	await expectNoOverlap(expanded, geolocate, '901: expanded assistant vs geolocate');
	const geolocateOwnsCentre = await geolocate.evaluate((element) => {
		const rect = element.getBoundingClientRect();
		return (
			document
				.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
				?.closest('button') === element
		);
	});
	expect(geolocateOwnsCentre).toBe(true);
	const closeOwnsCentre = await close.evaluate((element) => {
		const rect = element.getBoundingClientRect();
		return (
			document
				.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
				?.closest('button') === element
		);
	});
	expect(closeOwnsCentre).toBe(true);
});

test('desktop navigation and ordinary-page scrolling remain unchanged', async ({ page }) => {
	await page.setViewportSize({ width: 1024, height: 720 });
	await page.addInitScript(() =>
		localStorage.setItem(
			'moovelo:assistant-box',
			JSON.stringify({ collapsed: true, left: 120, top: 100 })
		)
	);
	await mockAuthenticatedPlanner(page, { assistant_enabled: true });
	await page.route('**/api/routes/tags', (route) => route.fulfill({ json: [] }));
	await page.route('**/api/routes', (route) => route.fulfill({ json: [] }));
	await page.goto('/');

	const nav = page.locator('nav');
	await expect(page.locator('.desktop-nav')).toBeVisible();
	await expect(page.locator('.mobile-menu-toggle')).toBeHidden();
	await expect(page.locator('.planner-guide')).toBeVisible();
	const desktopAssistant = page.locator('.assistant.collapsed');
	const desktopAssistantButton = desktopAssistant.getByRole('button', { name: 'Ask for a route' });
	await expect(desktopAssistantButton.locator('.assistant-pill-long')).toBeVisible();
	await expect(desktopAssistantButton.locator('.assistant-pill-short')).toBeHidden();
	expect(
		await desktopAssistantButton.evaluate((element) => (element as HTMLElement).innerText.trim())
	).toBe('Ask for a route');
	expect((await desktopAssistant.boundingBox())?.x).toBe(120);
	await expectNoOverlap(
		page.locator('.planner-guide'),
		page.locator('.toolbar'),
		'desktop toolbar'
	);
	const desktopStyle = await nav.evaluate((element) => {
		const style = getComputedStyle(element);
		return { height: element.getBoundingClientRect().height, background: style.backgroundColor };
	});
	expect(desktopStyle).toEqual({ height: 42, background: 'rgb(7, 54, 66)' });
	await page.setViewportSize({ width: 900, height: 720 });
	await expect(page.locator('.mobile-menu-toggle')).toBeVisible();
	expect(await nav.evaluate((element) => element.getBoundingClientRect().height)).toBe(44);
	await page.setViewportSize({ width: 901, height: 720 });
	await expect(page.locator('.desktop-nav')).toBeVisible();
	await expect(page.locator('.mobile-menu-toggle')).toBeHidden();
	expect(await nav.evaluate((element) => element.getBoundingClientRect().height)).toBe(42);
	await expect(desktopAssistantButton.locator('.assistant-pill-long')).toBeVisible();
	await expect(desktopAssistantButton.locator('.assistant-pill-short')).toBeHidden();
	expect((await desktopAssistant.boundingBox())?.x).toBe(120);

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
