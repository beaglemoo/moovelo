/// <reference types="@sveltejs/kit" />
/// <reference no-default-lib="true"/>
/// <reference lib="esnext" />
/// <reference lib="webworker" />

// SvelteKit injects the current build's asset list and a content-derived
// version string. The cache is keyed on that version, so every deploy installs
// a fresh cache and the old one is purged on activate.
//
// Update behaviour is deliberately conservative: no skipWaiting/clients.claim.
// A tab that is already open keeps its active worker (and its cache) until it
// is closed, and only then does the new version take over. Forcing the new
// worker to activate mid-session would purge the running tab's cache and then
// 404 on the next lazily-loaded route chunk, because a deploy replaces the
// hashed asset files on the server - breaking a live session is worse than
// letting it finish on the version it started with. A fresh load always gets
// the latest (navigations are network-first). The only cost is that a browser
// left open across many deploys accumulates one small per-version cache until
// it is closed; it self-heals on the next restart.
import { build, files, version } from '$service-worker';

const sw = self as unknown as ServiceWorkerGlobalScope;

const CACHE = `moovelo-cache-${version}`;

// build = the app's hashed, immutable JS/CSS; files = everything in static/
// (manifest, icons). Both are safe to precache and serve cache-first.
const PRECACHE = [...build, ...files];
const PRECACHE_SET = new Set(PRECACHE);

// The SPA document itself is not in build/files, and the navigation that
// registers the worker is never controlled by it - so without precaching the
// shell here, the first offline load (before any second, controlled
// navigation) would have nothing to fall back to and white-screen.
const SHELL = '/';

sw.addEventListener('install', (event) => {
	event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll([...PRECACHE, SHELL])));
});

sw.addEventListener('activate', (event) => {
	event.waitUntil(
		(async () => {
			for (const key of await caches.keys()) {
				if (key !== CACHE) await caches.delete(key);
			}
		})()
	);
});

sw.addEventListener('fetch', (event) => {
	const { request } = event;
	if (request.method !== 'GET') return;

	const url = new URL(request.url);

	// Never intercept cross-origin requests - this one rule keeps map tiles (and
	// any other third-party asset) out of the cache on every install shape, with
	// no tile-URL special-casing. And never the API: the app must always see
	// live data, and a cached API response would be worse than an error.
	if (url.origin !== sw.location.origin) return;
	if (url.pathname.startsWith('/api/')) return;

	event.respondWith(respond(request, url));
});

async function respond(request: Request, url: URL): Promise<Response> {
	const cache = await caches.open(CACHE);

	// Immutable, precached build assets and static files: cache-first.
	if (PRECACHE_SET.has(url.pathname)) {
		const cached = await cache.match(url.pathname);
		if (cached) return cached;
	}

	// Navigations: network-first so a live network always wins and a new deploy
	// is never masked by a cached page. Offline, fall back to the one precached
	// shell rather than caching a copy per visited URL - every route renders
	// from the same SPA document, so there is nothing per-URL worth storing.
	if (request.mode === 'navigate') {
		try {
			return await fetch(request);
		} catch (err) {
			const shell = await cache.match(SHELL);
			if (shell) return shell;
			throw err;
		}
	}

	// Any other same-origin GET (nothing dynamic today): network, with a cached
	// copy as the offline fallback. Nothing is written to the cache at runtime,
	// so the cache stays exactly the version-keyed precache - bounded, and
	// impossible to poison.
	try {
		return await fetch(request);
	} catch (err) {
		const cached = await cache.match(request);
		if (cached) return cached;
		throw err;
	}
}

export {};
