/// <reference types="@sveltejs/kit" />
/// <reference no-default-lib="true"/>
/// <reference lib="esnext" />
/// <reference lib="webworker" />

// SvelteKit injects the current build's asset list and a content-derived
// version string. The cache is keyed on that version, so every deploy lands in
// a fresh cache and the old one is purged on activate - no stale lock-in.
import { build, files, version } from '$service-worker';

const sw = self as unknown as ServiceWorkerGlobalScope;

const CACHE = `moovelo-cache-${version}`;

// build = the app's hashed, immutable JS/CSS; files = everything in static/
// (manifest, icons). Both are safe to precache and serve cache-first.
const PRECACHE = [...build, ...files];
const PRECACHE_SET = new Set(PRECACHE);

sw.addEventListener('install', (event) => {
	event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(PRECACHE)));
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

	// Never cache cross-origin requests - this one rule keeps map tiles (and any
	// other third-party asset) out of the cache on every install shape, with no
	// tile-URL special-casing. And never cache the API: the app must always see
	// live data, and an offline API response would be worse than an error.
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

	// Everything else - navigations and the SPA shell: network-first, falling
	// back to cache only when the network fails. A live network always wins, so
	// a new deploy is never masked by a cached page; an offline load still gets
	// the last good shell.
	try {
		const response = await fetch(request);
		if (response.status === 200 && response.type === 'basic') {
			cache.put(request, response.clone());
		}
		return response;
	} catch (err) {
		const cached = (await cache.match(request)) ?? (await cache.match('/'));
		if (cached) return cached;
		throw err;
	}
}

export {};
